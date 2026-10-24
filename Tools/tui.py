#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["textual>=0.80"]
# ///
"""Browse the Code tree, see (nested) submodule status and create folders.

Run with `just tui` or `uv run Tools/tui.py`.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from rich.markup import escape
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Checkbox, Footer, Header, Input, Label, OptionList, RichLog, Static, Tree
from textual.widgets.option_list import Option

ROOT = Path(__file__).resolve().parent.parent
GENERATOR = ROOT / "Tools" / "generator.py"


# --------------------------------------------------------------------------- git


def git(*args: str, cwd: Path = ROOT) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else ""


@dataclass
class RepoStatus:
    path: str  # relative to ROOT, "" for the root repo
    pointer: str = " "  # " " ok, "+" checkout differs from recorded, "-" not initialized, "U" conflict
    branch: str = ""
    upstream: str = ""
    ahead: int = 0
    behind: int = 0
    changed: int = 0
    untracked: int = 0
    changes: list[str] = field(default_factory=list)
    url: str = ""
    tracked_branch: str = ""

    @property
    def initialized(self) -> bool:
        return self.pointer != "-"

    @property
    def dirty(self) -> bool:
        return bool(self.changed or self.untracked)

    @property
    def detached(self) -> bool:
        return self.branch == "(detached)"

    @property
    def clean(self) -> bool:
        # a detached HEAD is a deliberate pin (third-party libs), being behind is fine there
        behind_ok = self.detached or not self.behind
        return self.initialized and not self.dirty and not self.ahead and behind_ok \
            and self.pointer == " "

    def badge(self) -> Text:
        t = Text()
        if not self.initialized:
            return t.append(" not initialized", "dim italic")
        if self.detached:
            t.append(f" @{self.tracked_branch or 'detached'}", "dim magenta")
        else:
            t.append(f" {self.branch}", "magenta")
        if self.tracked_branch and not self.detached and self.branch != self.tracked_branch:
            t.append(f"≠{self.tracked_branch}", "yellow")
        if self.ahead:
            t.append(f" ↑{self.ahead}", "cyan")
        if self.behind:
            t.append(f" ↓{self.behind}", "dim" if self.detached else "cyan")
        if self.changed:
            t.append(f" ●{self.changed}", "red")
        if self.untracked:
            t.append(f" ?{self.untracked}", "yellow")
        if self.pointer == "+":
            t.append(" ⇡ptr", "bright_yellow")
        if self.clean:
            t.append(" ✓", "green")
        return t


def list_submodules() -> dict[str, str]:
    """Map every (recursive) submodule path to its pointer state."""
    out = {}
    for line in git("submodule", "status", "--recursive").splitlines():
        if not line:
            continue
        state, rest = line[0], line[1:]
        parts = rest.split()
        if len(parts) >= 2:
            out[parts[1]] = state
    # nested submodules of uninitialized parents are not listed, that is fine
    return out


def tracked_branches() -> dict[str, tuple[str, str]]:
    """path -> (url, branch) from every .gitmodules in the tree."""
    out: dict[str, tuple[str, str]] = {}

    def read(repo: Path, prefix: str) -> None:
        cfg = repo / ".gitmodules"
        if not cfg.is_file():
            return
        entries: dict[str, dict[str, str]] = {}
        for line in git("config", "-f", str(cfg), "--get-regexp", r"^submodule\.").splitlines():
            key, _, value = line.partition(" ")
            name, _, attr = key[len("submodule."):].rpartition(".")
            entries.setdefault(name, {})[attr] = value
        for e in entries.values():
            if "path" not in e:
                continue
            path = f"{prefix}{e['path']}"
            out[path] = (e.get("url", ""), e.get("branch", ""))
            read(ROOT / path, f"{path}/")

    read(ROOT, "")
    return out


def repo_status(path: str, pointer: str, tracked: str = "") -> RepoStatus:
    st = RepoStatus(path=path, pointer=pointer, tracked_branch=tracked)
    if not st.initialized:
        return st
    cwd = ROOT / path
    out = git("status", "--porcelain=v2", "--branch", "--ignore-submodules=dirty", cwd=cwd)
    for line in out.splitlines():
        if line.startswith("# branch.head "):
            st.branch = line.split(" ", 2)[2]
        elif line.startswith("# branch.upstream "):
            st.upstream = line.split(" ", 2)[2]
        elif line.startswith("# branch.ab "):
            a, b = line.split()[2:4]
            st.ahead, st.behind = int(a), -int(b)
        elif line.startswith("? "):
            st.untracked += 1
            st.changes.append(f"?? {line[2:]}")
        elif line and not line.startswith("#"):
            st.changed += 1
            fields = line.split(" ")
            st.changes.append(f"{fields[1].replace('.', ' ')} {fields[-1]}")
    if st.detached:
        # submodule checkouts are detached by design: compare HEAD with the tracked remote branch
        branch = tracked or git("rev-parse", "--abbrev-ref", "origin/HEAD", cwd=cwd).strip().removeprefix("origin/")
        if branch and git("rev-parse", "--verify", "-q", f"origin/{branch}", cwd=cwd):
            st.tracked_branch = branch
            st.upstream = f"origin/{branch}"
            counts = git("rev-list", "--left-right", "--count", f"HEAD...origin/{branch}", cwd=cwd).split()
            if len(counts) == 2:
                st.ahead, st.behind = int(counts[0]), int(counts[1])
    st.url = git("remote", "get-url", "origin", cwd=cwd).strip()
    return st


def collect(progress=lambda done, total, msg: None, started=lambda paths: None,
            finished=lambda status: None) -> dict[str, RepoStatus]:
    progress(0, 0, "Listing submodules")
    pointers = list_submodules()
    meta = tracked_branches()
    pointers.setdefault("", " ")
    paths = list(pointers)
    started(paths)
    tracked = [meta.get(p, ("", ""))[1] for p in paths]
    statuses: dict[str, RepoStatus] = {}
    with ThreadPoolExecutor(max_workers=16) as pool:
        futures = {pool.submit(repo_status, p, pointers[p], t): p for p, t in zip(paths, tracked)}
        for done, future in enumerate(as_completed(futures), 1):
            st = statuses[futures[future]] = future.result()
            finished(st)
            progress(done, len(paths), "Reading status")
    for path, (url, branch) in meta.items():
        st = statuses.setdefault(path, RepoStatus(path=path, pointer="-"))
        st.tracked_branch = st.tracked_branch or branch
        st.url = st.url or url
    return statuses


# --------------------------------------------------------------------------- modals


class PromptScreen(ModalScreen[dict | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, placeholder: str, private_toggle: bool = False) -> None:
        super().__init__()
        self.title_text = title
        self.placeholder = placeholder
        self.private_toggle = private_toggle

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.title_text)
            yield Input(placeholder=self.placeholder, id="name")
            if self.private_toggle:
                yield Checkbox("Private repository", id="private")
            yield Label("[dim]enter to confirm, esc to cancel[/]")

    @on(Input.Submitted)
    def submit(self, event: Input.Submitted) -> None:
        name = event.value.strip()
        if not name:
            return
        private = self.query_one("#private", Checkbox).value if self.private_toggle else False
        self.dismiss({"name": name, "private": private})

    def action_cancel(self) -> None:
        self.dismiss(None)


class CommandScreen(ModalScreen[bool]):
    """Run shell commands inside the TUI and stream their output."""

    BINDINGS = [Binding("escape,q", "close", "Close")]

    def __init__(self, title: str, steps: list[tuple], keep_going: bool = False) -> None:
        """steps: (cmd, cwd) or (cmd, cwd, label). keep_going continues after failures."""
        super().__init__()
        self.title_text = title
        self.steps = steps
        self.keep_going = keep_going
        self.finished = False
        self.ok = True
        self.failed: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="cmd-dialog"):
            yield Label(Text(self.title_text, "bold"))
            yield RichLog(id="cmd-log", wrap=True, markup=False, highlight=False)
            yield Label("⠋ running… (esc closes when finished)", id="cmd-state")

    def on_mount(self) -> None:
        self.run_steps()

    @work(exclusive=True)
    async def run_steps(self) -> None:
        log = self.query_one(RichLog)
        state = self.query_one("#cmd-state", Label)
        total = len(self.steps)
        for index, (cmd, cwd, *label) in enumerate(self.steps, 1):
            where = "" if cwd == ROOT else f" ({cwd.relative_to(ROOT)})"
            title = label[0] if label else f"$ {shlex.join(cmd)}{where}"
            log.write(Text(title, "bold cyan"))
            if total > 1:
                state.update(f"⠋ running {index}/{total}: {title}")
            else:
                state.update("⠋ running… (esc closes when finished)")
            started = time.monotonic()
            env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -o BatchMode=yes"}
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            assert proc.stdout is not None
            async for raw in proc.stdout:
                log.write(Text(raw.decode(errors="replace").rstrip("\n").split("\r")[-1]))
            code = await proc.wait()
            if code != 0:
                self.ok = False
                self.failed.append(title)
                log.write(Text(f"  ✗ exit code {code}", "bold red"))
                if not self.keep_going:
                    break
        self.finished = True
        elapsed = f" in {time.monotonic() - started:.1f}s" if self.steps else ""
        if self.ok:
            state.update(Text(f"✓ done{elapsed}, esc to close", "green"))
        else:
            state.update(Text(f"✗ {len(self.failed)} failed, esc to close", "red"))
            log.write(Text("\nFailed:\n  " + "\n  ".join(self.failed), "red"))
        log.scroll_end(animate=False)

    def action_close(self) -> None:
        if self.finished:
            self.dismiss(self.ok)
        else:
            self.notify("Still running…", severity="warning")


class ChoiceScreen(ModalScreen[str | None]):
    """Pick one option with j/k/enter or its number."""

    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("j", "down", show=False),
                Binding("k", "up", show=False)]

    def __init__(self, title: str, body: Text | str, options: list[tuple[str, str]]) -> None:
        super().__init__()
        self.title_text = title
        self.body = body
        self.options = options

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(Text(self.title_text, "bold"))
            yield Static(self.body)
            yield OptionList(*[Option(f"{i}. {label}", id=key)
                               for i, (key, label) in enumerate(self.options, 1)])
            yield Label("[dim]enter or number to choose, esc to cancel[/]")

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    def on_key(self, event) -> None:
        if event.character and event.character.isdigit():
            index = int(event.character) - 1
            if 0 <= index < len(self.options):
                self.dismiss(self.options[index][0])

    @on(OptionList.OptionSelected)
    def chosen(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_down(self) -> None:
        self.query_one(OptionList).action_cursor_down()

    def action_up(self) -> None:
        self.query_one(OptionList).action_cursor_up()

    def action_cancel(self) -> None:
        self.dismiss(None)


# --------------------------------------------------------------------------- app


class CodeTUI(App):
    TITLE = "Code"
    CSS = """
    #tree { width: 45%; border-right: solid $primary-darken-2; }
    #detail { padding: 0 1; }
    #dialog { width: 60; height: auto; padding: 1 2; border: thick $primary; background: $surface; }
    PromptScreen, ChoiceScreen, CommandScreen { align: center middle; }
    #cmd-dialog { width: 90%; height: 80%; padding: 1 2; border: thick $primary; background: $surface; }
    #cmd-log { height: 1fr; border: round $panel; }
    ChoiceScreen OptionList { height: auto; max-height: 10; margin: 1 0; }
    #statusbar { dock: bottom; height: 1; padding: 0 1; background: $panel; }
    #statusbar.busy { background: $warning 30%; text-style: bold; }
    """
    BINDINGS = [
        # vim navigation (hidden from footer)
        Binding("j", "vim_down", "Down", show=False),
        Binding("k", "vim_up", "Up", show=False),
        Binding("h", "vim_left", "Collapse / parent", show=False),
        Binding("l", "vim_right", "Expand / open", show=False),
        Binding("g", "vim_top", "Top", show=False),
        Binding("G", "vim_bottom", "Bottom", show=False),
        Binding("ctrl+d", "vim_page(1)", "Half page down", show=False),
        Binding("ctrl+u", "vim_page(-1)", "Half page up", show=False),
        Binding("n", "new_folder", "New folder"),
        Binding("s", "new_submodule", "New submodule"),
        Binding("r", "refresh", "Refresh"),
        Binding("f", "fetch", "Fetch all"),
        Binding("u", "update", "Update here"),
        Binding("U", "update_all", "Update all"),
        Binding("c", "pointer", "Commit pointer"),
        Binding("C", "commit_all_pointers", "Commit all pointers"),
        Binding("p", "push_here", "Push here"),
        Binding("P", "push_all", "Push all"),
        Binding("o", "shell", "Shell here"),
        Binding("e", "editor", "Editor"),
        Binding("d", "toggle_dirty", "Only dirty"),
        Binding("a", "toggle_files", "Files"),
        Binding("period", "toggle_hidden", "Hidden"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.statuses: dict[str, RepoStatus] = {}
        self.show_hidden = False
        self.only_dirty = False
        self.show_files = True
        # None = submodule list not known yet (everything is loading), else paths still being read
        self.pending: set[str] | None = None
        self.loading = False

    def query_one(self, *args, **kwargs):
        """Always look up main-screen widgets, even while a modal is on top."""
        main = self.screen_stack[0] if self.screen_stack else self.screen
        return main.query_one(*args, **kwargs)

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            tree = Tree(Text(ROOT.name, "bold"), data=ROOT, id="tree")
            tree.auto_expand = False  # enter is handled in node_selected
            yield tree
            with VerticalScroll():
                yield Static(id="detail")
        yield Static(id="statusbar")
        yield Footer()

    def on_mount(self) -> None:
        tree = self.query_one(Tree)
        tree.focus()
        self.populate(tree.root)  # show folders immediately, git status fills in later
        tree.root.expand()
        self.set_interval(0.1, self.tick)
        self.action_refresh()

    # ------------------------------------------------------------------ status bar

    SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    busy_msg: str | None = None
    spin = 0

    def set_busy(self, msg: str | None) -> None:
        self.busy_msg = msg
        self.query_one("#statusbar").set_class(msg is not None, "busy")
        self.tick()

    def report(self, done: int, total: int, msg: str) -> None:
        """Thread-safe progress update from workers."""
        text = f"{msg} {done}/{total}" if total else msg
        self.call_from_thread(self.set_busy, text)

    def tick(self) -> None:
        bar = self.query_one("#statusbar", Static)
        if self.busy_msg is not None:
            self.spin = (self.spin + 1) % len(self.SPINNER)
            bar.update(f"{self.SPINNER[self.spin]} {self.busy_msg}…")
        else:
            bar.update(Text(self.idle_text))
        if self.loading:
            self.relabel()

    idle_text = ""

    def relabel(self) -> None:
        tree = self.query_one(Tree)
        for node in self.walk(tree.root):
            if node.data is not None:
                node.set_label(self.label_for(node.data))

    def is_loading(self, path: Path) -> bool:
        if not self.loading or not path.is_dir():
            return False
        if self.pending is None:
            return True
        rel = self.rel(path)
        prefix = f"{rel}/" if rel else ""
        return rel in self.pending or any(p.startswith(prefix) for p in self.pending if p)

    def loading_started(self, paths: list[str]) -> None:
        self.call_from_thread(setattr, self, "pending", set(paths))

    def loading_finished(self, st: RepoStatus) -> None:
        def apply() -> None:
            if self.pending is not None:
                self.pending.discard(st.path)
            self.statuses[st.path] = st
        self.call_from_thread(apply)

    # ------------------------------------------------------------------ helpers

    def rel(self, path: Path) -> str:
        return "" if path == ROOT else path.relative_to(ROOT).as_posix()

    def cursor_path(self) -> Path:
        node = self.query_one(Tree).cursor_node
        return node.data if node and node.data else ROOT

    def selected_path(self) -> Path:
        """Directory under the cursor (parent dir when a file is selected)."""
        path = self.cursor_path()
        return path if path.is_dir() else path.parent

    def descendants(self, rel: str) -> list[RepoStatus]:
        prefix = f"{rel}/" if rel else ""
        return [s for p, s in self.statuses.items() if p and p.startswith(prefix) and p != rel]

    def label_for(self, path: Path) -> Text:
        rel = self.rel(path)
        st = self.statuses.get(rel)
        if not path.is_dir():
            return Text(f"   {path.name}", "dim" if path.name.startswith(".") else "")
        if path == ROOT:
            label = Text(ROOT.name, "bold")
        elif st:
            label = Text(f"📦 {path.name}", "bold")
        else:
            label = Text(f"📁 {path.name}")
        if st:
            label.append_text(st.badge())
        nested = self.descendants(rel)
        dirty = sum(1 for s in nested if not s.clean and s.initialized)
        if nested and dirty:
            label.append(f"  ({dirty} nested need attention)", "dim yellow")
        if self.is_loading(path):
            label.append(f" {self.SPINNER[self.spin]}", "bold yellow")
        return label

    def needs_attention(self, path: Path) -> bool:
        rel = self.rel(path)
        own = self.statuses.get(rel)
        if own and own.initialized and not own.clean:
            return True
        return any(s.initialized and not s.clean for s in self.descendants(rel))

    def children(self, path: Path) -> list[Path]:
        try:
            entries = list(path.iterdir())
        except OSError:
            return []
        items = sorted((p for p in entries if p.is_dir() or (self.show_files and p.is_file())),
                       key=lambda p: (not p.is_dir(), p.name.lower()))
        items = [p for p in items if p.name != ".git" and (self.show_hidden or not p.name.startswith("."))]
        if self.only_dirty:
            items = [p for p in items if self.needs_attention(p)]
        return items

    def populate(self, node) -> None:
        node.remove_children()
        for child in self.children(node.data):
            if child.is_dir():
                expandable = os.access(child, os.R_OK) and bool(self.children(child))
                node.add(self.label_for(child), data=child, allow_expand=expandable)
            else:
                node.add_leaf(self.label_for(child), data=child)

    def rebuild(self) -> None:
        tree = self.query_one(Tree)
        expanded = {n.data for n in self.walk(tree.root) if n.is_expanded}
        cursor = tree.cursor_node.data if tree.cursor_node else None
        tree.root.set_label(self.label_for(ROOT))
        self._restore(tree.root, expanded)
        if cursor:
            for n in self.walk(tree.root):
                if n.data == cursor:
                    tree.move_cursor(n)
                    break
        self.show_detail()

    def _restore(self, node, expanded: set) -> None:
        self.populate(node)
        for child in node.children:
            if child.data in expanded:
                child.expand()
                self._restore(child, expanded)

    def walk(self, node):
        yield node
        for child in node.children:
            yield from self.walk(child)

    # ------------------------------------------------------------------ events

    @on(Tree.NodeExpanded)
    def expanded(self, event: Tree.NodeExpanded) -> None:
        if event.node is not self.query_one(Tree).root and not event.node.children:
            self.populate(event.node)

    @on(Tree.NodeHighlighted)
    def highlighted(self, _event) -> None:
        self.show_detail()

    @on(Tree.NodeSelected)
    def node_selected(self, event: Tree.NodeSelected) -> None:
        """Enter: toggle folders, open files in $EDITOR."""
        path = event.node.data or ROOT
        if path.is_dir():
            if event.node is not self.query_one(Tree).root:
                event.node.toggle()
        else:
            self.action_editor()

    # ------------------------------------------------------------------ vim keys

    def action_vim_down(self) -> None:
        self.query_one(Tree).action_cursor_down()

    def action_vim_up(self) -> None:
        self.query_one(Tree).action_cursor_up()

    def action_vim_top(self) -> None:
        self.query_one(Tree).action_scroll_home()

    def action_vim_bottom(self) -> None:
        self.query_one(Tree).action_scroll_end()

    def action_vim_page(self, direction: int) -> None:
        tree = self.query_one(Tree)
        step = max(1, tree.size.height // 2)
        tree.cursor_line = max(0, min(tree.last_line, tree.cursor_line + direction * step))

    def action_vim_left(self) -> None:
        tree = self.query_one(Tree)
        node = tree.cursor_node
        if node is None:
            return
        if node.is_expanded and node is not tree.root:
            node.collapse()
        elif node.parent is not None:
            tree.move_cursor(node.parent)

    def action_vim_right(self) -> None:
        tree = self.query_one(Tree)
        node = tree.cursor_node
        if node is None:
            return
        path = node.data or ROOT
        if not path.is_dir():
            self.action_editor()
        elif not node.is_expanded and node.allow_expand:
            node.expand()
        elif node.children:
            tree.move_cursor(node.children[0])

    # ------------------------------------------------------------------ detail

    def show_detail(self) -> None:
        cursor = self.cursor_path()
        if not cursor.is_dir():
            out = Table.grid(padding=(0, 1))
            out.add_column(style="bold")
            out.add_column()
            out.add_row("File", escape(self.rel(cursor)))
            try:
                out.add_row("Size", f"{cursor.stat().st_size:,} bytes")
            except OSError:
                pass
            out.add_row("", Text("enter / l: open in $EDITOR, o: shell in folder", "dim"))
            self.query_one("#detail", Static).update(out)
            return
        path = cursor
        rel = self.rel(path)
        st = self.statuses.get(rel)
        out = Table.grid(padding=(0, 1))
        out.add_column(style="bold")
        out.add_column()
        out.add_row("Path", escape(rel or "."))
        if self.is_loading(path):
            out.add_row("Status", Text("loading…", "yellow"))
        out.add_row("Type", "submodule" if st and rel else ("root repo" if not rel else "folder"))
        if st:
            out.add_row("Remote", escape(st.url or "-"))
            out.add_row("Branch", st.badge())
            if st.tracked_branch:
                out.add_row("Tracks", st.tracked_branch)
            if st.upstream:
                out.add_row("Upstream", f"{st.upstream} (↑{st.ahead} ↓{st.behind})")
            if st.pointer == "+":
                out.add_row("Pointer", Text("checkout differs from commit recorded in parent (c to commit or reset)",
                                            "yellow"))
            if st.changes:
                out.add_row("Changes", Text("\n".join(st.changes[:25])
                                            + (f"\n… {len(st.changes) - 25} more" if len(st.changes) > 25 else ""),
                                            "red"))

        nested = self.descendants(rel)
        parts: list = [out]
        if nested:
            table = Table(title=f"Nested submodules ({len(nested)})", title_justify="left",
                          expand=True, show_edge=False)
            table.add_column("Path")
            table.add_column("Status")
            for s in sorted(nested, key=lambda s: s.path):
                table.add_row(escape(s.path[len(rel) + 1 if rel else 0:]), s.badge())
            parts.append(table)

        from rich.console import Group
        self.query_one("#detail", Static).update(Group(*parts))

    # ------------------------------------------------------------------ actions

    def begin_loading(self) -> None:
        self.pending = None
        self.loading = True

    def collect_with_progress(self) -> dict[str, RepoStatus]:
        self.call_from_thread(self.begin_loading)
        return collect(self.report, self.loading_started, self.loading_finished)

    @work(thread=True, exclusive=True, group="status")
    def action_refresh(self) -> None:
        self.call_from_thread(self.apply_statuses, self.collect_with_progress())

    def apply_statuses(self, statuses: dict[str, RepoStatus]) -> None:
        self.loading = False
        self.pending = set()
        self.statuses = statuses
        subs = [s for p, s in statuses.items() if p]
        dirty = sum(1 for s in subs if s.initialized and not s.clean)
        self.sub_title = f"{len(subs)} submodules, {dirty} need attention"
        self.idle_text = f"✓ {len(subs)} submodules, {dirty} need attention"
        self.rebuild()
        self.set_busy(None)

    @work(thread=True, exclusive=True, group="status")
    def action_fetch(self) -> None:
        self.report(0, 0, "Fetching all submodules")
        self.call_from_thread(self.begin_loading)
        git("submodule", "foreach", "--recursive", "--quiet", "git fetch --quiet --prune || true")
        self.call_from_thread(self.apply_statuses, self.collect_with_progress())

    def run_in_terminal(self, cmd: list[str], cwd: Path = ROOT, pause: bool = True) -> None:
        """Interactive programs (editor, shell, gh) need the real terminal."""
        with self.suspend():
            subprocess.run(cmd, cwd=cwd)
            if pause:
                input("\nPress enter to return…")
        self.action_refresh()

    def run_in_tui(self, title: str, steps: list[tuple], keep_going: bool = False) -> None:
        """Non-interactive commands: stream output in a modal, refresh after."""
        self.push_screen(CommandScreen(title, steps, keep_going), lambda _ok: self.action_refresh())

    # parallel fetch of every (nested) remote, then an offline merge: ~10x faster than
    # `update --remote`, whose fetches run one by one even with --jobs
    UPDATE_SCRIPT = r'''
snap() { git -C "$ROOT" submodule foreach --quiet --recursive 'echo "$displaypath $(git rev-parse --short HEAD)"'; }
snap > "$TMPDIR/code-tui-before.$$"
if [ $# -gt 0 ]; then target="$1"; else target=.; fi
echo "fetching in parallel (16 jobs)…"
git -C "$target" fetch --recurse-submodules=yes --jobs 16 --quiet || echo "  some fetches failed, continuing with what we have"
echo "merging tracked branches…"
if [ $# -gt 0 ]; then
    git submodule update --remote --no-fetch --merge --recursive -- "$1"
else
    git submodule update --remote --no-fetch --merge --recursive
fi
code=$?
snap > "$TMPDIR/code-tui-after.$$"
echo
awk 'NR==FNR { before[$1]=$2; next } before[$1]!=$2 { printf "  updated %s %s -> %s\n", $1, (before[$1]=="" ? "new" : before[$1]), $2; n++ }
     END { if (!n) print "  everything already up to date"; else printf "\n%d submodule(s) updated\n", n }' \
    "$TMPDIR/code-tui-before.$$" "$TMPDIR/code-tui-after.$$"
rm -f "$TMPDIR/code-tui-before.$$" "$TMPDIR/code-tui-after.$$"
exit $code
'''

    def update_steps(self, scope: str) -> list[tuple]:
        """Update `scope` (or everything when empty) and all submodules nested in it."""
        args: list[str] = []
        cwd = ROOT
        if scope:
            parent = self.parent_of(scope)
            cwd = ROOT / parent
            args = [scope[len(parent) + 1:] if parent else scope]
        label = f"$ git fetch --recurse-submodules --jobs 16 && git submodule update --remote --no-fetch " \
                f"--merge --recursive {' '.join(args)}".rstrip()
        env_cmd = ["env", f"ROOT={ROOT}", f"TMPDIR={os.environ.get('TMPDIR', '/tmp')}",
                   "sh", "-c", self.UPDATE_SCRIPT, "update", *args]
        return [(env_cmd, cwd, label)]

    def owning_submodule(self, rel: str) -> str:
        """The submodule the path belongs to (itself if it is one), or "" for the root repo."""
        return rel if rel in self.statuses else self.parent_of(rel)

    def action_update(self) -> None:
        scope = self.owning_submodule(self.rel(self.selected_path()))
        if not scope:
            self.action_update_all()
            return
        self.run_in_tui(f"Update {scope} (recursive)", self.update_steps(scope))

    def action_update_all(self) -> None:
        self.run_in_tui("Update all submodules (recursive)", self.update_steps(""))

    # ------------------------------------------------------------------ pointers

    def parent_of(self, rel: str) -> str:
        """Closest repo (submodule or root) that contains `rel`."""
        candidates = [p for p in self.statuses if p and rel.startswith(f"{p}/")]
        return max(candidates, key=len) if candidates else ""

    def ancestors(self, rel: str) -> list[str]:
        chain, current = [], rel
        while current:
            current = self.parent_of(current)
            chain.append(current)
        return chain  # nearest parent first, root ("") last

    def commit_steps(self, rel: str, all_parents: bool) -> list[tuple[list[str], Path]]:
        steps, child = [], rel
        for parent in self.ancestors(rel):
            inner = child[len(parent) + 1:] if parent else child
            msg = f"chore({Path(inner).name.lower()}): bump {inner} submodule"
            cwd = ROOT / parent
            steps.append((["git", "add", "--", inner], cwd))
            steps.append((["git", "commit", "-m", msg, "--", inner], cwd))
            if not all_parents:
                break
            child = parent
        return steps

    def commit_all_plan(self) -> list[tuple[str, list[str]]]:
        """(repo, [child paths relative to repo]) to commit, deepest repo first.

        Every repo between a drifted submodule and the root gets one commit that
        records all its changed children, so the whole chain ends up consistent.
        """
        drifted = [p for p, s in self.statuses.items() if p and s.pointer == "+"]
        children: dict[str, set[str]] = {}
        for rel in drifted:
            child = rel
            for parent in self.ancestors(rel):
                children.setdefault(parent, set()).add(child)
                child = parent
        order = sorted(children, key=lambda p: (-(p.count("/") + 1 if p else 0), p))
        return [(repo, sorted(c[len(repo) + 1:] if repo else c for c in children[repo])) for repo in order]

    COMMIT_SCRIPT = r'''
git add -- "$@" || exit $?
if git diff --cached --quiet -- "$@"; then echo "  nothing to commit"; exit 0; fi
git commit --quiet -m "$MSG" -m "$BODY" -- "$@" || exit $?
echo "  committed $(git rev-parse --short HEAD)"
'''

    def action_commit_all_pointers(self) -> None:
        plan = self.commit_all_plan()
        if not plan:
            self.notify("All submodule pointers match their parents")
            return
        body = Text()
        for repo, kids in plan:
            body.append(f"{repo or 'root'}\n", "bold")
            for kid in kids:
                body.append(f"  • {kid}\n", "dim")
        body.append("\nOne commit per repo, deepest first. Only submodule pointers are committed,\n"
                    "other staged or modified files are left alone.", "italic")
        steps = []
        for repo, kids in plan:
            names = ", ".join(Path(k).name.lower() for k in kids)
            msg = f"chore: bump {names} submodule{'s' if len(kids) > 1 else ''}"
            if len(msg) > 72:
                msg = f"chore: bump {len(kids)} submodules"
            detail = "\n".join(f"- {k}" for k in kids)
            steps.append((["env", f"MSG={msg}", f"BODY={detail}", "sh", "-c", self.COMMIT_SCRIPT, "commit", *kids],
                          ROOT / repo, f"→ {repo or 'root'}: {msg}"))

        def done(choice: str | None) -> None:
            if choice == "go":
                self.run_in_tui("Commit all submodule pointers up to root", steps)

        self.push_screen(ChoiceScreen(f"Commit pointers in {len(plan)} repos", body,
                                      [("go", "Commit all"), ("cancel", "Cancel")]), done)

    # ------------------------------------------------------------------ push

    PUSH_SCRIPT = r'''
branch=$(git branch --show-current)
if git rev-parse --verify -q "@{u}" >/dev/null; then
    git push --quiet origin "$branch" || exit $?
else
    git push --quiet --set-upstream origin "$branch" || exit $?
fi
echo "  pushed $branch ($(git rev-parse --short HEAD))"
'''

    def push_plan(self, scope: str | None) -> tuple[list[str], list[str]]:
        """Repos to push (deepest first, root last) and detached repos with unpushed commits.

        scope None = everything including root, "" or a path = that repo and everything inside.
        """
        def inside(p: str) -> bool:
            if scope is None or scope == "":
                return True
            return p == scope or p.startswith(f"{scope}/")

        ahead, stranded = [], []
        for p, s in self.statuses.items():
            if not s.initialized or not inside(p) or s.ahead == 0:
                continue
            (stranded if s.detached else ahead).append(p)
        ahead.sort(key=lambda p: (-(p.count("/") + 1 if p else 0), p))
        return ahead, sorted(stranded)

    def run_push(self, title: str, scope: str | None) -> None:
        repos, stranded = self.push_plan(scope)
        if stranded:
            self.notify("Detached with local commits, not pushed:\n" + "\n".join(stranded),
                        severity="warning", timeout=8)
        if not repos:
            self.notify("Nothing to push")
            return
        steps = []
        for repo in repos:
            st = self.statuses[repo]
            steps.append((["bash", "-c", self.PUSH_SCRIPT], ROOT / repo,
                          f"→ {repo or 'root'} ({st.branch}, {st.ahead} commit{'s' if st.ahead != 1 else ''})"))
        # nested first: a parent must never point at a commit its remote can't serve
        self.run_in_tui(title, steps)

    def action_push_here(self) -> None:
        scope = self.owning_submodule(self.rel(self.selected_path()))
        self.run_push(f"Push {scope or 'root'} and everything inside", scope)

    def action_push_all(self) -> None:
        self.run_push("Push everything, deepest first, root last", None)

    def action_pointer(self) -> None:
        rel = self.rel(self.selected_path())
        st = self.statuses.get(rel)
        if not rel or not st or st.pointer != "+":
            self.notify("Selected submodule matches the commit recorded in its parent")
            return
        parents = self.ancestors(rel)
        nearest = parents[0] or "root"
        chain = " → ".join(p or "root" for p in parents)
        recorded = git("ls-tree", "HEAD", "--", rel[len(parents[0]) + 1:] if parents[0] else rel,
                       cwd=ROOT / parents[0]).split()
        recorded_sha = recorded[2][:9] if len(recorded) > 2 else "?"
        current = git("rev-parse", "--short=9", "HEAD", cwd=ROOT / rel).strip()
        log = git("log", "--oneline", "-8", f"{recorded_sha}..HEAD", cwd=ROOT / rel).strip()
        body = Text()
        body.append(f"{rel}\n", "bold")
        body.append(f"recorded in {nearest}: {recorded_sha}\ncheckout: {current}\n")
        if log:
            body.append(f"\n{log}\n", "dim")
        options = [("one", f"Commit pointer in {nearest} only")]
        if len(parents) > 1:
            options.append(("all", f"Commit in all parents ({chain})"))
        options.append(("reset", f"Discard: check out recorded commit {recorded_sha}"))

        def done(choice: str | None) -> None:
            if choice in ("one", "all"):
                self.run_in_tui(f"Commit {rel} pointer", self.commit_steps(rel, choice == "all"))
            elif choice == "reset":
                inner = rel[len(parents[0]) + 1:] if parents[0] else rel
                self.run_in_tui(f"Reset {rel}", [(["git", "submodule", "update", "--init", "--", inner],
                                                   ROOT / parents[0])])

        self.push_screen(ChoiceScreen("Checkout differs from recorded commit", body, options), done)

    def action_shell(self) -> None:
        self.run_in_terminal([os.environ.get("SHELL", "/bin/sh")], cwd=self.selected_path(), pause=False)

    def action_editor(self) -> None:
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
        cursor = self.cursor_path()
        target = "." if cursor.is_dir() else cursor.name
        self.run_in_terminal([*editor.split(), target], cwd=self.selected_path(), pause=False)

    def action_toggle_files(self) -> None:
        self.show_files = not self.show_files
        self.notify("Showing files" if self.show_files else "Showing folders only")
        self.rebuild()

    def action_toggle_hidden(self) -> None:
        self.show_hidden = not self.show_hidden
        self.rebuild()

    def action_toggle_dirty(self) -> None:
        self.only_dirty = not self.only_dirty
        self.notify("Showing only paths that need attention" if self.only_dirty else "Showing everything")
        self.rebuild()

    def action_new_folder(self) -> None:
        parent = self.selected_path()

        def done(result: dict | None) -> None:
            if not result:
                return
            target = parent / result["name"]
            try:
                target.mkdir(parents=True)
            except OSError as exc:
                self.notify(str(exc), severity="error")
                return
            self.notify(f"Created {self.rel(target)}")
            self.reveal(target)

        self.push_screen(PromptScreen(f"New folder in {self.rel(parent) or '.'}/", "Name or nested/path"), done)

    def action_new_submodule(self) -> None:
        parent = self.selected_path()
        prefix = self.rel(parent)
        # the generator adds submodules to the root repo, so the target must not live inside a submodule
        owner = next((p for p in self.statuses if p and (prefix == p or prefix.startswith(f"{p}/"))), None)
        if owner:
            self.notify(f"{prefix} is inside submodule {owner}, creating at root level instead", severity="warning")
            prefix = ""

        def done(result: dict | None) -> None:
            if not result:
                return
            folder = f"{prefix}/{result['name']}" if prefix else result["name"]
            cmd = [sys.executable, str(GENERATOR), folder]
            if result["private"]:
                cmd.append("--private")
            self.run_in_terminal(cmd)
            self.reveal(ROOT / folder)

        self.push_screen(PromptScreen(f"New submodule in {prefix or '.'}/ (creates GitHub repo)",
                                      "Folder name, e.g. Zig", private_toggle=True), done)

    def reveal(self, target: Path) -> None:
        tree = self.query_one(Tree)
        node = tree.root
        for part in target.relative_to(ROOT).parts:
            node.expand()
            self.populate(node) if not node.children else None
            match = next((c for c in node.children if c.data and c.data.name == part), None)
            if match is None:
                self.populate(node)
                match = next((c for c in node.children if c.data and c.data.name == part), None)
            if match is None:
                return
            node = match
        # new nodes get their line numbers on the next refresh
        self.call_after_refresh(tree.move_cursor, node)


if __name__ == "__main__":
    CodeTUI().run()
