# Code

My development folder structure. Each topic lives in its own repository and is
linked here as a git submodule, so the whole tree can be recreated anywhere.

## Clone

Submodule URLs use SSH, so a GitHub SSH key is required.

```sh
git clone --recurse-submodules -j8 git@github.com:erayaydin/code.git Code
```

Already cloned without submodules:

```sh
git submodule update --init --recursive -j8
```

## Daily usage

Recipes live in the `justfile` (requires [just](https://github.com/casey/just)).

```sh
just setup    # one-time: recommended git settings for submodules
just init     # clone missing submodules
just update   # pull latest commits of every submodule's tracked branch
just update X # same, limited to path X
just status   # show uncommitted changes inside submodules
just bump     # commit updated submodule pointers in this repo (or `just bump <root>`)
just attach   # put submodules back on main/master after a detached checkout
just tui      # interactive browser with recursive submodule status
```

Recipes are thin wrappers around `Tools/repos.py` (plain `python3`, shared with the TUI).
Add `--dry-run` to any of them to print the plan without running it.

Ignored folders can hold separate private repos ("private roots", git repos directly under
an ignored parent or listed in a local `.code.local`). Recipes and the TUI handle each root
on its own, so nothing about them is recorded here. A root's `.code-root.toml` can set
`protect = ["Projects/**"]`: matching repos are never committed, pushed, attached or merged
by the tooling, updates only fast-forward clean checkouts.

Top-level submodules live on their tracked branch (`main`, or `master` for archived
projects) so they can be committed to directly. Nested third-party submodules stay
detached on purpose: they are pinned versions.

## Adding a new module

```sh
just new Zig              # creates github.com/erayaydin/zig and adds Zig/ as submodule
just new Zig --private    # private repo
just new Zig --dry-run    # preview commands
```

For an existing repo:

```sh
just new Zig --existing   # only adds github.com/erayaydin/zig as Zig/
```

In a private root nothing is created, an existing project is added (bare names go to
`Projects/`, owner and SSH host come from `[generator]` in the root's `.code-root.toml`):

```sh
just new Company/Acme/some-project    # adds <owner>/some-project as Projects/some-project
```
