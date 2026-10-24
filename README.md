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
just status   # show uncommitted changes inside submodules
just bump     # commit updated submodule pointers in this repo
just attach   # put submodules back on main/master after a detached checkout
just tui      # interactive browser with recursive submodule status
```

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
git submodule add -b main git@github.com:erayaydin/<repo>.git <Folder>
```
