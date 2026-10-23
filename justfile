# Submodule workflow helpers. Run `just` to list recipes.

default:
    @just --list

# Clone missing submodules (after a plain git clone)
init:
    git submodule update --init --recursive --jobs 8

# Pull latest commits of every submodule's tracked branch
update:
    git submodule update --remote --merge --recursive --jobs 8

# Show uncommitted changes inside submodules
status:
    git submodule foreach --recursive --quiet 'out=$(git status --short); [ -z "$out" ] || printf "\n== %s\n%s\n" "$displaypath" "$out"'

# Commit updated submodule pointers in this repo
bump:
    git add $(git config -f .gitmodules --get-regexp '\.path$' | awk '{print $2}')
    git diff --cached --quiet || git commit -m "chore: bump submodules" -m "$(git diff --cached --submodule=short | grep '^Submodule')"

# Enable recommended git settings for this repo
setup:
    git config submodule.recurse true
    git config status.submoduleSummary true
    git config diff.submodule log
    git config fetch.parallel 8
