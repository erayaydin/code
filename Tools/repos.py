#!/usr/bin/env python3
"""Repo model and plans shared by the TUI and the justfile.

Finds Code plus every private root (ignored git repos such as Company/<Name>), reads
(nested) submodule status, applies `protect` patterns from a root's .code-root.toml and
builds command plans. A plan is a list of steps `(cmd, cwd, label)`, run by the TUI in a
modal or by `main()` in the terminal. Nothing here depends on Textual or rich, so it runs
with a plain `python3` (3.11+ for tomllib).

    python3 Tools/repos.py <command> [args] [--dry-run]
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# private meta repos live in ignored folders and are never submodules of Code
PRIVATE_PARENTS = ("Company",)
PRIVATE_EXTRA = ("Projects/Secret",)
LOCAL_ROOTS_FILE = ".code.local"
# per private root config, e.g. `protect = ["Projects/**"]`: the tooling never commits, pushes,
# attaches, merges or resets inside matching repos, it only fast-forwards clean ones
ROOT_CONFIG = ".code-root.toml"

Step = tuple  # (cmd: list[str], cwd: Path, label: str)


# --------------------------------------------------------------------------- git


def git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=cwd or ROOT, capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else ""


@dataclass
class RepoStatus:
    path: str  # relative to ROOT, "" for the root repo
    root: str = ""  # repo root this belongs to: "" for Code, else a private root like Company/Fingerprint
    protected: bool = False  # matches a `protect` pattern of its root: sync (fast-forward) only
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


# --------------------------------------------------------------------------- discovery


def is_repo_root(path: Path) -> bool:
    """True when `path` is the top of its own git repo (not just a folder inside Code)."""
    if not path.is_dir() or not (path / ".git").exists():
        return False
    top = git("rev-parse", "--show-toplevel", cwd=path).strip()
    return bool(top) and Path(top).resolve() == path.resolve()


def discover_roots() -> list[str]:
    """Private meta repos: every repo directly under Company/, the extras, and .code.local lines."""
    candidates: list[str] = []
    for parent in PRIVATE_PARENTS:
        base = ROOT / parent
        if base.is_dir():
            candidates += sorted(f"{parent}/{p.name}" for p in base.iterdir() if p.is_dir())
    candidates += PRIVATE_EXTRA
    local = ROOT / LOCAL_ROOTS_FILE
    if local.is_file():
        for line in local.read_text().splitlines():
            line = line.split("#", 1)[0].strip().strip("/")
            if line:
                candidates.append(line)
    seen: list[str] = []
    for rel in candidates:
        if rel not in seen and is_repo_root(ROOT / rel):
            seen.append(rel)
    return seen


def list_submodules(root: str = "") -> dict[str, str]:
    """Map every (recursive) submodule path of `root` to its pointer state, paths relative to ROOT."""
    out = {}
    prefix = f"{root}/" if root else ""
    for line in git("submodule", "status", "--recursive", cwd=ROOT / root).splitlines():
        if not line:
            continue
        state, rest = line[0], line[1:]
        parts = rest.split()
        if len(parts) >= 2:
            out[f"{prefix}{parts[1]}"] = state
    # nested submodules of uninitialized parents are not listed, that is fine
    return out


def tracked_branches(root: str = "") -> dict[str, tuple[str, str]]:
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

    read(ROOT / root, f"{root}/" if root else "")
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


def root_config(root: str) -> dict:
    """`.code-root.toml` of a private root, {} when missing or broken."""
    cfg = ROOT / root / ROOT_CONFIG
    if not root or not cfg.is_file():
        return {}
    try:
        return tomllib.loads(cfg.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def matches_protect(inner: str, patterns: list[str]) -> bool:
    """`inner` (relative to its root) is protected by a pattern. `X/**` covers everything below X."""
    for pat in patterns:
        pat = pat.strip().strip("/")
        if pat.endswith("/**"):
            base = pat[:-3]
            parts = inner.split("/")
            # any proper ancestor-or-self below `base` level counts: Projects/a and Projects/a/lib
            if any(fnmatchcase("/".join(parts[:i]), base) for i in range(1, len(parts))):
                return True
        elif fnmatchcase(inner, pat):
            return True
    return False


def collect(progress=lambda done, total, msg: None, started=lambda paths: None,
            finished=lambda status: None) -> dict[str, RepoStatus]:
    progress(0, 0, "Listing submodules")
    pointers: dict[str, str] = {}
    meta: dict[str, tuple[str, str]] = {}
    private = discover_roots()
    for root in ["", *private]:
        pointers[root] = " "  # a root is never recorded in a parent, so it can't drift
        pointers.update(list_submodules(root))
        meta.update(tracked_branches(root))
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
    roots = sorted(private, key=len, reverse=True)
    protect = {r: list(root_config(r).get("protect", [])) for r in private}
    for path, st in statuses.items():
        st.root = next((r for r in roots if path == r or path.startswith(f"{r}/")), "")
        if st.root and path != st.root:
            st.protected = matches_protect(path[len(st.root) + 1:], protect[st.root])
    return statuses


# --------------------------------------------------------------------------- scripts

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

# protected repos: never merge, only fast-forward a clean checkout to its tracked remote branch
FF_SCRIPT = r'''
b="$BRANCH"
[ -n "$b" ] || b=$(git rev-parse --abbrev-ref origin/HEAD 2>/dev/null | sed 's#^origin/##')
[ -z "$b" ] || git fetch --quiet origin "$b" 2>/dev/null || echo "  $P: fetch failed, using last fetched origin/$b"
if [ -z "$b" ] || ! git rev-parse -q --verify "origin/$b" >/dev/null; then echo "  skip $P: no tracked remote branch"; exit 0; fi
if [ -n "$(git status --porcelain --ignore-submodules=all)" ]; then echo "  skip $P: uncommitted changes"; exit 0; fi
cur=$(git branch --show-current)
if [ -n "$cur" ] && [ "$cur" != "$b" ]; then echo "  skip $P: on branch $cur, tracks $b"; exit 0; fi
if git merge-base --is-ancestor "origin/$b" HEAD; then
    ahead=$(git rev-list --count "origin/$b..HEAD")
    if [ "$ahead" -gt 0 ]; then echo "  skip $P: $ahead local commit(s) ahead"; else echo "  $P up to date"; fi
    exit 0
fi
if ! git merge-base --is-ancestor HEAD "origin/$b"; then echo "  skip $P: diverged from origin/$b"; exit 0; fi
old=$(git rev-parse --short HEAD)
git merge --ff-only --quiet "origin/$b" || exit $?
echo "  fast-forwarded $P $old -> $(git rev-parse --short HEAD)"
'''

# commits only the given submodule paths, other staged or modified files are left alone
COMMIT_SCRIPT = r'''
git add -- "$@" || exit $?
if git diff --cached --quiet -- "$@"; then echo "  nothing to commit"; exit 0; fi
[ -n "$BODY" ] || BODY=$(git diff --cached --submodule=short -- "$@" | grep '^Submodule')
git commit --quiet -m "$MSG" -m "$BODY" -- "$@" || exit $?
echo "  committed $(git rev-parse --short HEAD)"
'''

PUSH_SCRIPT = r'''
branch=$(git branch --show-current)
if git rev-parse --verify -q "@{u}" >/dev/null; then
    git push --quiet origin "$branch" || exit $?
else
    git push --quiet --set-upstream origin "$branch" || exit $?
fi
echo "  pushed $branch ($(git rev-parse --short HEAD))"
'''

ATTACH_SCRIPT = r'''
git checkout -q "$B" && git branch -q -u "origin/$B" "$B" && echo "  $P -> $B"
'''


def depth(p: str) -> int:
    return p.count("/") + 1 if p else 0


# --------------------------------------------------------------------------- model


class Workspace:
    """Statuses of Code and every private root, plus every plan built on them."""

    def __init__(self, statuses: dict[str, RepoStatus] | None = None) -> None:
        self.statuses: dict[str, RepoStatus] = statuses or {}

    def reload(self) -> Workspace:
        self.statuses = collect()
        return self

    # ------------------------------------------------------------------ graph

    def roots(self) -> list[str]:
        """Code ("") first, then every private root found by collect()."""
        return ["", *sorted({s.root for s in self.statuses.values() if s.root})]

    def parent_of(self, rel: str) -> str:
        """Closest repo (submodule or root) that contains `rel`."""
        candidates = [p for p in self.statuses if p and rel.startswith(f"{p}/")]
        return max(candidates, key=len) if candidates else ""

    def ancestors(self, rel: str) -> list[str]:
        st = self.statuses.get(rel)
        stop = st.root if st else ""
        chain, current = [], rel
        while current and current != stop:
            current = self.parent_of(current)
            chain.append(current)
        return chain  # nearest parent first, its root last (never crosses into Code)

    def owning_submodule(self, rel: str) -> str:
        """The submodule the path belongs to (itself if it is one), or "" for the root repo."""
        return rel if rel in self.statuses else self.parent_of(rel)

    def descendants(self, rel: str) -> list[RepoStatus]:
        prefix = f"{rel}/" if rel else ""
        return [s for p, s in self.statuses.items() if p and p.startswith(prefix) and p != rel]

    def top_level(self, root: str) -> list[RepoStatus]:
        """Submodules recorded directly in `root` (not nested deeper, not other roots)."""
        return sorted((s for p, s in self.statuses.items()
                       if p and p != s.root and s.root == root and self.parent_of(p) == root),
                      key=lambda s: s.path)

    def is_guarded(self, rel: str) -> bool:
        """Tooling must not commit, push, attach, merge or reset in this repo (protect pattern of its root)."""
        st = self.statuses.get(rel)
        return bool(st and st.protected)

    def resolve(self, scope: str) -> str:
        """User given path ("." / "Code" / trailing slash) to a status key."""
        scope = scope.strip().strip("/")
        return "" if scope in (".", "") else scope

    # ------------------------------------------------------------------ update

    def update_steps(self, scope: str) -> list[Step]:
        """Update `scope` and all submodules nested in it. Empty scope = Code and every private root."""
        if not scope:
            return [step for root in self.roots() for step in self.update_steps_in(root, None)]
        st = self.statuses.get(scope)
        if st and scope == st.root:
            return self.update_steps_in(scope, None)
        parent = self.parent_of(scope)
        return self.update_steps_in(parent, scope[len(parent) + 1:] if parent else scope)

    def update_steps_in(self, repo: str, inner: str | None) -> list[Step]:
        """Run the update script in `repo`, limited to submodule `inner` (relative to repo) if given."""
        prefix = f"{repo}/" if repo else ""
        scope = f"{prefix}{inner}" if inner else repo
        if any(s.protected and (p == scope or p.startswith(f"{scope}/")) for p, s in self.statuses.items()):
            return self.protected_update_steps(repo, inner)
        args = [inner] if inner else []
        cwd = ROOT / repo
        where = f"[{repo}] " if repo else ""
        label = f"$ {where}git fetch --recurse-submodules --jobs 16 && git submodule update --remote --no-fetch " \
                f"--merge --recursive {' '.join(args)}".rstrip()
        env_cmd = ["env", f"ROOT={cwd}", f"TMPDIR={os.environ.get('TMPDIR', '/tmp')}",
                   "sh", "-c", UPDATE_SCRIPT, "update", *args]
        return [(env_cmd, cwd, label)]

    def protected_update_steps(self, repo: str, inner: str | None) -> list[Step]:
        """Update a private root (or a part of it) that has protected repos.

        Unprotected subtrees use the normal merge update, protected repos only fast-forward,
        parents first so nested repos see their new recorded pins.
        """
        prefix = f"{repo}/" if repo else ""
        scope = f"{prefix}{inner}" if inner else repo
        inside = [p for p in self.statuses if p != repo and (p == scope or p.startswith(f"{scope}/"))]
        inside.sort(key=lambda p: (p.count("/"), p))
        steps: list[Step] = [(["sh", "-c", "git fetch --recurse-submodules=yes --jobs 16 --quiet || true"],
                              ROOT / (scope if scope in self.statuses else repo),
                              f"$ [{scope}] git fetch --recurse-submodules --jobs 16")]
        covered: list[str] = []
        for p in inside:
            if any(p.startswith(f"{c}/") for c in covered):
                continue
            st = self.statuses[p]
            parent = self.parent_of(p)
            rel_in_parent = p[len(parent) + 1:] if parent else p
            if st.protected:
                if not st.initialized:
                    steps.append((["echo", f"  skip {p}: not initialized"], ROOT, f"🛡 {p}"))
                    continue
                steps.append((["env", f"BRANCH={st.tracked_branch}", f"P={p}", "sh", "-c", FF_SCRIPT],
                              ROOT / p, f"🛡 {p}: fast-forward only"))
                continue
            has_protected = any(s.protected for q, s in self.statuses.items() if q.startswith(f"{p}/"))
            cmd = ["git", "submodule", "update", "--remote", "--no-fetch", "--merge", "--init"]
            if not has_protected:
                cmd.append("--recursive")
                covered.append(p)
            steps.append(([*cmd, "--", rel_in_parent], ROOT / parent, f"$ [{parent}] {' '.join(cmd)} {rel_in_parent}"))
        return steps

    # ------------------------------------------------------------------ commit

    def commit_steps(self, rel: str, all_parents: bool) -> list[tuple[list[str], Path]]:
        steps, child = [], rel
        for parent in self.ancestors(rel):
            if self.is_guarded(parent):
                break
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

        Every repo between a drifted submodule and its root gets one commit that
        records all its changed children, so the whole chain ends up consistent.
        """
        drifted = [p for p, s in self.statuses.items() if p and s.pointer == "+"]
        children: dict[str, set[str]] = {}
        for rel in drifted:
            child = rel
            for parent in self.ancestors(rel):
                if self.is_guarded(parent):
                    break  # never commit inside a protected repo, nor above it on its behalf
                children.setdefault(parent, set()).add(child)
                child = parent
        order = sorted(children, key=lambda p: (-depth(p), p))
        return [(repo, sorted(c[len(repo) + 1:] if repo else c for c in children[repo])) for repo in order]

    def commit_all_steps(self, plan: list[tuple[str, list[str]]] | None = None) -> list[Step]:
        steps = []
        for repo, kids in self.commit_all_plan() if plan is None else plan:
            names = ", ".join(Path(k).name.lower() for k in kids)
            msg = f"chore: bump {names} submodule{'s' if len(kids) > 1 else ''}"
            if len(msg) > 72:
                msg = f"chore: bump {len(kids)} submodules"
            detail = "\n".join(f"- {k}" for k in kids)
            steps.append((["env", f"MSG={msg}", f"BODY={detail}", "sh", "-c", COMMIT_SCRIPT, "commit", *kids],
                          ROOT / repo, f"→ {repo or 'root'}: {msg}"))
        return steps

    def bump_steps(self, root: str) -> list[Step]:
        """Commit every changed top-level pointer of `root` in one commit (the `just bump` recipe)."""
        if self.is_guarded(root):
            return [(["sh", "-c", f"echo '  {root} is protected, not committing' >&2; exit 1"], ROOT, f"🛡 {root}")]
        kids = [s.path[len(root) + 1 if root else 0:] for s in self.top_level(root) if s.initialized]
        if not kids:
            return []
        return [(["env", "MSG=chore: bump submodules", "BODY=", "sh", "-c", COMMIT_SCRIPT, "bump", *kids],
                 ROOT / root, f"→ {root or 'root'}: chore: bump submodules")]

    # ------------------------------------------------------------------ push

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
            if not s.initialized or not inside(p) or s.ahead == 0 or self.is_guarded(p):
                continue
            (stranded if s.detached else ahead).append(p)
        ahead.sort(key=lambda p: (-depth(p), p))
        return ahead, sorted(stranded)

    def push_steps(self, repos: list[str]) -> list[Step]:
        # nested first: a parent must never point at a commit its remote can't serve
        steps = []
        for repo in repos:
            st = self.statuses[repo]
            steps.append((["bash", "-c", PUSH_SCRIPT], ROOT / repo,
                          f"→ {repo or 'root'} ({st.branch}, {st.ahead} commit{'s' if st.ahead != 1 else ''})"))
        return steps

    # ------------------------------------------------------------------ setup

    def attach_steps(self) -> list[Step]:
        """Put top-level submodules of every root on their tracked branch, protected ones untouched."""
        steps = []
        for root in self.roots():
            for st in self.top_level(root):
                if not st.initialized:
                    continue
                if st.protected:
                    steps.append((["echo", f"  {st.path}: protected, left as is"], ROOT, f"🛡 {st.path}"))
                elif not st.tracked_branch:
                    steps.append((["echo", f"  {st.path}: no tracked branch"], ROOT, f"· {st.path}"))
                elif st.branch == st.tracked_branch:
                    continue
                else:
                    steps.append((["env", f"B={st.tracked_branch}", f"P={st.path}", "sh", "-c", ATTACH_SCRIPT],
                                  ROOT / st.path, f"→ {st.path}: attach {st.tracked_branch}"))
        return steps

    def setup_steps(self) -> list[Step]:
        """Recommended git config for every root. Protected repos get update=none, not merge."""
        steps = []
        for root in self.roots():
            cwd = ROOT / root
            for key, value in (("submodule.recurse", "true"), ("status.submoduleSummary", "true"),
                               ("diff.submodule", "log"), ("fetch.parallel", "8")):
                steps.append((["git", "config", key, value], cwd, f"$ [{root or 'root'}] git config {key} {value}"))
            for st in self.top_level(root):
                inner = st.path[len(root) + 1 if root else 0:]
                mode = "none" if st.protected else "merge"
                steps.append((["git", "config", f"submodule.{inner}.update", mode], cwd,
                              f"$ [{root or 'root'}] git config submodule.{inner}.update {mode}"))
        return steps

    def init_steps(self) -> list[Step]:
        """Clone submodules that are not initialized yet, only where their parent is checked out.

        Existing checkouts are never touched. Nested ones show up after their parent is cloned,
        so `main()` runs this in rounds.
        """
        steps = []
        for p, st in sorted(self.statuses.items()):
            if st.initialized or not p:
                continue
            parent = self.parent_of(p)
            if parent and not self.statuses[parent].initialized:
                continue
            inner = p[len(parent) + 1:] if parent else p
            steps.append((["git", "submodule", "update", "--init", "--checkout", "--", inner],
                          ROOT / parent, f"→ {p}: clone"))
        return steps

    def status_report(self) -> str:
        """Uncommitted changes in every repo of every root (the `just status` recipe)."""
        out = []
        for p, st in sorted(self.statuses.items()):
            if st.initialized and st.changes:
                out.append(f"\n== {p or 'root'}{' 🛡' if st.protected else ''}")
                out += st.changes
        return "\n".join(out)


# --------------------------------------------------------------------------- cli


def run(steps: list[Step], dry_run: bool = False, keep_going: bool = False) -> int:
    code = 0
    for cmd, cwd, *label in steps:
        title = label[0] if label else " ".join(cmd)
        if dry_run:
            where = os.path.relpath(cwd, ROOT)
            shown = [c if "\n" not in c else "<script>" for c in cmd]
            print(f"{title}\n    (in {where}) {' '.join(shown)}")
            continue
        print(title, flush=True)
        rc = subprocess.run(cmd, cwd=cwd).returncode
        if rc:
            print(f"  ✗ exit code {rc}", file=sys.stderr)
            code = rc
            if not keep_going:
                break
    return code


USAGE = """usage: repos.py <command> [args] [--dry-run]

  roots              Code (.) and every private root, one per line
  protected          protected repo paths, one per line
  status             uncommitted changes in every repo of every root
  update [scope]     pull latest (merge), protected repos only fast-forward
  bump [root]        commit changed top-level pointers of a root (default Code)
  attach             put top-level submodules on their tracked branch (skips protected)
  setup              recommended git config for every root, then attach
  init               clone missing submodules (never resets existing checkouts), then attach

--dry-run prints the plan without running anything.
"""


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    args = [a for a in argv if a != "--dry-run"]
    if not args or args[0] in ("-h", "--help"):
        print(USAGE, end="")
        return 0 if args else 2
    cmd, rest = args[0], args[1:]
    if cmd == "roots":
        print("\n".join([".", *discover_roots()]))
        return 0
    ws = Workspace().reload()
    if cmd == "protected":
        print("\n".join(sorted(p for p, s in ws.statuses.items() if s.protected)))
        return 0
    if cmd == "status":
        report = ws.status_report()
        print(report.lstrip("\n") if report else "everything clean")
        return 0
    if cmd == "update":
        return run(ws.update_steps(ws.resolve(rest[0] if rest else "")), dry)
    if cmd == "bump":
        root = ws.resolve(rest[0] if rest else "")
        if root not in ws.roots():
            print(f"{root} is not a root, roots: {', '.join(r or '.' for r in ws.roots())}", file=sys.stderr)
            return 2
        return run(ws.bump_steps(root), dry)
    if cmd == "attach":
        return run(ws.attach_steps(), dry, keep_going=True)
    if cmd == "setup":
        return run(ws.setup_steps(), dry, keep_going=True) or run(ws.attach_steps(), dry, keep_going=True)
    if cmd == "init":
        for _ in range(8):  # nested submodules appear once their parent is cloned
            steps = ws.init_steps()
            if not steps:
                break
            code = run(steps, dry, keep_going=True)
            if dry or code:
                return code
            ws.reload()
        return run(ws.attach_steps(), dry, keep_going=True)
    print(f"unknown command {cmd!r}\n\n{USAGE}", end="", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
