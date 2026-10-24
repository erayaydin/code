# Submodule workflow helpers. Run `just` to list recipes.
# Logic lives in Tools/repos.py (shared with the TUI), every recipe accepts --dry-run.
# Private roots (git repos under Company/, Projects/Secret, lines in .code.local) are handled
# one by one and never recorded in Code. Repos matching `protect` in a root's .code-root.toml
# are never committed, pushed, attached or merged.

repos := "python3 Tools/repos.py"

default:
    @just --list

# Clone missing submodules in Code and every private root (never resets existing checkouts)
init *flags:
    {{repos}} init {{flags}}

# Pull latest of every tracked branch, recursive. Protected repos only fast-forward. Optional scope path
update *args:
    {{repos}} update {{args}}

# Show uncommitted changes in every repo of every root
status:
    {{repos}} status

# Commit updated submodule pointers in a root repo (Code by default, or e.g. Company/Fingerprint)
bump *args:
    {{repos}} bump {{args}}

# Create a GitHub repo and add it as a submodule
new folder *flags:
    python3 Tools/generator.py {{folder}} {{flags}}

# Browse folders and recursive submodule status
tui:
    uv run -q --script Tools/tui.py

# Put top-level submodules of each root on their tracked branch (skips protected repos)
attach *flags:
    {{repos}} attach {{flags}}

# Recommended git settings for every root (protected repos get update=none, not merge)
setup *flags:
    {{repos}} setup {{flags}}
