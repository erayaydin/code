#!/usr/bin/env python3
"""Create a new topic folder as a git submodule.

Steps:
  1. Scaffold the folder structure in a temporary directory.
  2. Create an initial commit on the target branch.
  3. Create the GitHub repository with `gh` and push.
  4. Add it to this repo with `git submodule add -b <branch>`.

Example:
  Tools/generator.py Zig
  Tools/generator.py DeepLearning --repo deep-learning --private
  Tools/generator.py Zig --dry-run
"""

import argparse
import logging
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

SUBFOLDERS = ("Tutorials", "Examples", "Projects", "Libraries", "Articles",
              "Tools", "Notes")

ROOT = Path(__file__).resolve().parent.parent
logger = logging.getLogger("generator")


def kebab(name):
    return re.sub(r"(?<=[a-z])(?=[A-Z][a-z])", "-", name).lower()


def run(cmd, cwd=None, dry_run=False, capture=False):
    logger.info("$ %s", shlex.join(cmd))
    if dry_run:
        return ""
    result = subprocess.run(cmd, cwd=cwd, check=True, text=True,
                            capture_output=capture)
    return result.stdout.strip() if capture else ""


def repo_exists(slug):
    return subprocess.run(["gh", "repo", "view", slug],
                          capture_output=True).returncode == 0


def scaffold(path, folder, subfolders):
    (path / "README.md").write_text(f"# {folder}\n")
    for sub in subfolders:
        (path / sub).mkdir()
        (path / sub / ".gitkeep").touch()


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", help="folder name, e.g. Zig or DeepLearning")
    parser.add_argument("--repo", help="GitHub repo name (default: kebab-case of folder)")
    parser.add_argument("--owner", help="GitHub owner (default: authenticated gh user)")
    parser.add_argument("--branch", default="main", help="branch to track (default: main)")
    parser.add_argument("--private", action="store_true", help="create a private repo")
    parser.add_argument("--no-scaffold", action="store_true",
                        help="only create README.md, skip the standard subfolders")
    parser.add_argument("--dry-run", action="store_true", help="print commands without running them")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug output")
    return parser.parse_args()


def main():
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(message)s")

    target = ROOT / args.folder
    if target.exists():
        sys.exit(f"error: {target} already exists")

    owner = args.owner or run(["gh", "api", "user", "--jq", ".login"], capture=True)
    repo = args.repo or kebab(Path(args.folder).name)
    slug = f"{owner}/{repo}"
    url = f"git@github.com:{slug}.git"

    if not args.dry_run and repo_exists(slug):
        sys.exit(f"error: github.com/{slug} already exists, add it manually:\n"
                 f"  git submodule add -b {args.branch} {url} {args.folder}")

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        scaffold(work, args.folder, () if args.no_scaffold else SUBFOLDERS)
        run(["git", "init", "-q", "-b", args.branch], cwd=work, dry_run=args.dry_run)
        run(["git", "add", "."], cwd=work, dry_run=args.dry_run)
        run(["git", "commit", "-q", "-m", "chore: initial structure"],
            cwd=work, dry_run=args.dry_run)
        run(["gh", "repo", "create", slug,
             "--private" if args.private else "--public",
             "--source", ".", "--remote", "origin", "--push"],
            cwd=work, dry_run=args.dry_run)

    run(["git", "submodule", "add", "-b", args.branch, url, args.folder],
        cwd=ROOT, dry_run=args.dry_run)

    if args.dry_run:
        return
    print(f"\n{args.folder} added as submodule ({url}). Commit it with:\n"
          f"  git commit -m \"feat({repo}): add {args.folder} submodule\"")


if __name__ == "__main__":
    main()
