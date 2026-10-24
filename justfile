# Submodule workflow helpers. Run `just` to list recipes.

default:
    @just --list

# Clone missing submodules (after a plain git clone)
init:
    git submodule update --init --recursive --jobs 8
    @just attach

# Pull latest commits of every submodule's tracked branch (recursive, parallel fetch)
update:
    git fetch --recurse-submodules=yes --jobs 16
    git submodule update --remote --no-fetch --merge --recursive

# Show uncommitted changes inside submodules
status:
    git submodule foreach --recursive --quiet 'out=$(git status --short); [ -z "$out" ] || printf "\n== %s\n%s\n" "$displaypath" "$out"'

# Commit updated submodule pointers in this repo
bump:
    git add $(git config -f .gitmodules --get-regexp '\.path$' | awk '{print $2}')
    git diff --cached --quiet || git commit -m "chore: bump submodules" -m "$(git diff --cached --submodule=short | grep '^Submodule')"

# Create a GitHub repo and add it as a submodule
new folder *flags:
    python3 Tools/generator.py {{folder}} {{flags}}

# Browse folders and recursive submodule status
tui:
    uv run --script Tools/tui.py

# Put every top-level submodule on its tracked branch instead of a detached HEAD
attach:
    git submodule foreach --quiet 'b=$(git config -f "$toplevel/.gitmodules" "submodule.$name.branch"); git checkout -q "$b" && git branch -q -u "origin/$b" "$b" && echo "$name -> $b"'

# Enable recommended git settings for this repo
setup:
    git config submodule.recurse true
    git config status.submoduleSummary true
    git config diff.submodule log
    git config fetch.parallel 8
    git config -f .gitmodules --get-regexp '\.path$' | awk '{print $2}' | while read -r p; do git config "submodule.$p.update" merge; done
    @just attach
