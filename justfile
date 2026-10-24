# Submodule workflow helpers. Run `just` to list recipes.
# Private roots (git repos under Company/, Projects/Secret, lines in .code.local) are handled
# one by one and never recorded in Code. Repos matching `protect` in a root's .code-root.toml
# are never committed, pushed, attached or merged by these recipes.

tui := "uv run -q --script Tools/tui.py"

default:
    @just --list

# Clone missing submodules in Code and every private root (never resets existing checkouts)
init:
    #!/bin/sh
    for r in $({{tui}} roots); do
        echo "== $r"
        for round in 1 2 3 4 5 6; do
            missing=$(git -C "$r" submodule status --recursive | awk '/^-/ {print $2}')
            [ -n "$missing" ] || break
            for p in $missing; do
                parent=$(dirname "$p"); top=$(git -C "$r/$parent" rev-parse --show-toplevel)
                git -C "$top" submodule update --init --checkout -- "$(cd "$r" && pwd)/$p"
            done
        done
    done
    just attach

# Pull latest of every tracked branch, recursive. Protected repos only fast-forward. Optional scope path
update scope="":
    {{tui}} update {{scope}}

# Show uncommitted changes inside submodules, per root
status:
    #!/bin/sh
    for r in $({{tui}} roots); do
        R="$r" git -C "$r" submodule foreach --recursive --quiet 'out=$(git status --short); p="$R/$displaypath"; [ -z "$out" ] || printf "\n== %s\n%s\n" "${p#./}" "$out"'
    done

# Commit updated submodule pointers in a root repo (Code by default, or e.g. Company/Fingerprint)
bump root=".":
    #!/bin/sh
    set -e
    cd "{{root}}"
    paths=$(git config -f .gitmodules --get-regexp '\.path$' | awk '{print $2}')
    [ -n "$paths" ] || exit 0
    git add $paths
    git diff --cached --quiet || git commit -m "chore: bump submodules" -m "$(git diff --cached --submodule=short | grep '^Submodule')"

# Create a GitHub repo and add it as a submodule
new folder *flags:
    python3 Tools/generator.py {{folder}} {{flags}}

# Browse folders and recursive submodule status
tui:
    {{tui}}

# Put every top-level submodule of each root on its tracked branch (skips protected repos)
attach:
    #!/bin/sh
    protected=$({{tui}} protected)
    for r in $({{tui}} roots); do
        PROTECTED="$protected" R="$r" git -C "$r" submodule foreach --quiet '
            full="$R/$sm_path"; full="${full#./}"
            if printf "%s\n" "$PROTECTED" | grep -qxF "$full"; then echo "$full: protected, left as is"; exit 0; fi
            b=$(git config -f "$toplevel/.gitmodules" "submodule.$name.branch")
            [ -n "$b" ] || { echo "$full: no tracked branch"; exit 0; }
            git checkout -q "$b" && git branch -q -u "origin/$b" "$b" && echo "$full -> $b"'
    done

# Recommended git settings for every root (protected repos get update=none, not merge)
setup:
    #!/bin/sh
    protected=$({{tui}} protected)
    for r in $({{tui}} roots); do
        git -C "$r" config submodule.recurse true
        git -C "$r" config status.submoduleSummary true
        git -C "$r" config diff.submodule log
        git -C "$r" config fetch.parallel 8
        git -C "$r" config -f .gitmodules --get-regexp '\.path$' 2>/dev/null | awk '{print $2}' | while read -r p; do
            full="$r/$p"; full="${full#./}"
            if printf "%s\n" "$protected" | grep -qxF "$full"; then
                git -C "$r" config "submodule.$p.update" none
            else
                git -C "$r" config "submodule.$p.update" merge
            fi
        done
    done
    just attach
