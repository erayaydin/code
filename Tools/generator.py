#!/usr/bin/env python3
"""Create a new topic folder as a git submodule of Code, or add a company project to a private root.

In Code (topics):
  1. Scaffold the folder structure in a temporary directory.
  2. Create an initial commit on the target branch.
  3. Create the GitHub repository with `gh` and push.
  4. Add it to Code with `git submodule add -b <branch>`.

In a private root (Company/<Name>, detected from the path or given with --root) nothing is
created: an existing repo (a company project) is only added as a submodule, by default under
Projects/. Use --existing in Code for the same behavior there. Defaults come from the root's
.code-root.toml:

    [generator]
    owner = "fingerprintjs"        # GitHub owner (default: authenticated gh user)
    host = "github.com"            # SSH host, e.g. an ~/.ssh/config alias "github-fp"
    dir = "Projects"               # folder for bare project names (default Projects)

Example:
  Tools/generator.py Zig
  Tools/generator.py DeepLearning --repo deep-learning --private
  Tools/generator.py Company/Fingerprint/fingerprint-pro-server-api-node-sdk
  Tools/generator.py dx-toolbox --root Company/Fingerprint --owner fingerprintjs
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from repos import ROOT, ROOT_CONFIG, discover_roots, root_config  # noqa: E402

SUBFOLDERS = ("Tutorials", "Examples", "Projects", "Libraries", "Articles",
              "Tools", "Notes")

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


def parse_repo(value):
    """(owner or None, name) from `name`, `name.git`, `owner/name`, git@host:owner/name.git or https URLs."""
    value = value.strip().rstrip("/").removesuffix(".git")
    if "://" in value:
        value = value.split("://", 1)[1].split("/", 1)[-1]
    elif value.startswith("git@") and ":" in value:
        value = value.split(":", 1)[1]
    owner, _, name = value.rpartition("/")
    return owner.rsplit("/", 1)[-1] or None, name


def scaffold(path, folder, subfolders):
    (path / "README.md").write_text(f"# {Path(folder).name}\n")
    for sub in subfolders:
        (path / sub).mkdir()
        (path / sub / ".gitkeep").touch()


def split_root(folder, root):
    """(root, folder relative to root). root "" = Code. Accepts paths from Code or from the root."""
    folder = folder.strip("/")
    if root is None:
        root = next((r for r in sorted(discover_roots(), key=len, reverse=True)
                     if folder.startswith(f"{r}/")), "")
    root = root.strip("/")
    if root in (".", ""):
        return "", folder
    if root not in discover_roots():
        sys.exit(f"error: {root} is not a private root (a git repo under Company/, "
                 f"Projects/Secret or listed in .code.local)")
    return root, folder[len(root) + 1:] if folder.startswith(f"{root}/") else folder


def parse_args():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", help="folder, e.g. Zig, Company/Fingerprint/Tools/x or a path inside --root")
    parser.add_argument("--root", help="private root to add the submodule to (default: detected from folder)")
    parser.add_argument("--repo", help="GitHub repo: name, name.git, owner/name or clone URL (default: kebab-case "
                                        "of folder name in Code, the folder name as is in a private root)")
    parser.add_argument("--owner", help="GitHub owner (default: root config, else authenticated gh user)")
    parser.add_argument("--host", help="SSH host for the submodule URL (default: root config or github.com)")
    parser.add_argument("--branch", default="main", help="branch to track (default: main)")
    parser.add_argument("--private", action="store_true", help="create a private repo (Code topics only)")
    parser.add_argument("--existing", action="store_true",
                        help="repo already exists on GitHub: only add it as a submodule (always on in private roots)")
    parser.add_argument("--no-scaffold", action="store_true",
                        help="only create README.md, skip the standard subfolders")
    parser.add_argument("--dry-run", action="store_true", help="print commands without running them")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug output")
    return parser.parse_args()


def main():
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(message)s")

    root, folder = split_root(args.folder, args.root)
    cfg = root_config(root).get("generator", {}) if root else {}
    existing = args.existing or bool(root)  # no topics in private roots, only existing projects
    if root and "/" not in folder:
        folder = f"{cfg.get('dir', 'Projects')}/{folder}"
    repo_dir = ROOT / root
    target = repo_dir / folder
    if not Path(folder).name:
        sys.exit("error: folder is empty")
    if target.exists():
        sys.exit(f"error: {target} already exists")

    repo_owner, repo_name = parse_repo(args.repo) if args.repo else (None, None)
    owner = args.owner or repo_owner or cfg.get("owner") or run(["gh", "api", "user", "--jq", ".login"], capture=True)
    owner = owner.strip().strip("/")
    host = args.host or cfg.get("host", "github.com")
    repo = repo_name or (Path(folder).name if root else kebab(Path(folder).name))
    slug = f"{owner}/{repo}"
    url = f"git@{host}:{slug}.git"
    where = root or "Code"
    logger.info("# %s -> %s in %s (%s)", folder, slug, where, "existing repo" if existing else
                ("new private repo" if args.private else "new public repo"))

    exists = not args.dry_run and repo_exists(slug)
    if existing:
        if not args.dry_run and not exists:
            sys.exit(f"error: github.com/{slug} not found (or no access), check --owner/--repo")
    elif exists:
        sys.exit(f"error: github.com/{slug} already exists, add it with --existing:\n"
                 f"  {Path(sys.argv[0]).name} {args.folder} --existing")
    else:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            scaffold(work, folder, () if args.no_scaffold else SUBFOLDERS)
            run(["git", "init", "-q", "-b", args.branch], cwd=work, dry_run=args.dry_run)
            run(["git", "add", "."], cwd=work, dry_run=args.dry_run)
            run(["git", "commit", "-q", "-m", "chore: first initialize of the project"],
                cwd=work, dry_run=args.dry_run)
            run(["gh", "repo", "create", slug,
                 "--private" if args.private else "--public",
                 "--source", ".", "--remote", "origin", "--push"],
                cwd=work, dry_run=args.dry_run)

    run(["git", "submodule", "add", "-b", args.branch, url, folder],
        cwd=repo_dir, dry_run=args.dry_run)

    if args.dry_run:
        return
    cd = f"cd {root} && " if root else ""
    print(f"\n{folder} added as submodule of {where} ({url}). Commit it with:\n"
          f"  {cd}git commit -m \"feat({repo}): add {Path(folder).name} submodule\"")
    if root and not (repo_dir / ROOT_CONFIG).is_file():
        print(f"\nTip: add {root}/{ROOT_CONFIG} with `protect = [\"Projects/**\"]` and a [generator] "
              f"owner, see {Path(__file__).name} --help")


if __name__ == "__main__":
    main()
