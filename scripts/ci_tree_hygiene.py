"""Tree hygiene: the two whole-index guards ANY change set can break, run on every pull request.

`tests/quality/test_no_tracked_ignored_files.py` (no tracked path matches .gitignore) and
`tests/quality/test_filetree.py` (FILETREE.md maps the tracked tree) read git's index, not a
module, so a change that only adds a file can break them. ci.yml's `scope` job used to run
nothing for a change set it called documentation: be05b4b5ab (#12784) changed a spec and an
`adws/` plan, came out of scope with 0 gated jobs, and turned main red at 1ef009cae5. The
`scope` job now runs this module on every pull request, in or out of scope, with the runner's
own `python3` (standard library only, about 1 s), and both test modules call these checks so
the logic lives once.

[if] a pull request tracks a gitignored path, or leaves FILETREE.md out of step with the index
[then] `python3 -m scripts.ci_tree_hygiene` exits 1 naming each path, [else stop].
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TRACKED_IGNORED_BASELINE = Path("tests/quality/fixtures/tracked_ignored_baseline.txt")
FILETREE = Path("FILETREE.md")
LISTED = re.compile(r"^\s*- `([^`]+)`", re.MULTILINE)
COMPLETE_CHILDREN = ("apps", "ops", "tools", "ci")
COMPLETE_SUBFOLDERS = ("docs",)


# ----- git index --------------------------------------------------------------


def _git_lines(root: Path, *args: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line]


def tracked_paths(root: Path) -> list[str]:
    """Every tracked path; 1000 or fewer is a broken read, never a small tree."""
    tracked = _git_lines(root, "ls-files")
    if len(tracked) <= 1000:
        raise RuntimeError(f"control: expected the whole tree, read {len(tracked)} paths")
    return tracked


# ----- tracked but ignored ------------------------------------------------------


def tracked_ignored_paths(root: Path) -> set[str]:
    return set(_git_lines(root, "ls-files", "-ci", "--exclude-per-directory=.gitignore"))


def tracked_ignored_baseline(root: Path) -> set[str]:
    lines = (root / TRACKED_IGNORED_BASELINE).read_text(encoding="utf-8").splitlines()
    return {line for line in lines if line and not line.startswith("#")}


def new_tracked_ignored(root: Path) -> list[str]:
    return sorted(tracked_ignored_paths(root) - tracked_ignored_baseline(root))


def stale_tracked_ignored_baseline(root: Path) -> list[str]:
    return sorted(tracked_ignored_baseline(root) - tracked_ignored_paths(root))


# ----- FILETREE.md --------------------------------------------------------------


def tree_entries(tracked: list[str]) -> set[str]:
    """Every tracked file plus every tracked directory, directories with a trailing '/'."""
    entries = set(tracked)
    for path in tracked:
        parts = path.split("/")[:-1]
        entries |= {"/".join(parts[: i + 1]) + "/" for i in range(len(parts))}
    return entries


def _children(entries: set[str], parent: str, dirs_only: bool) -> set[str]:
    prefix = parent + "/"
    return {
        entry
        for entry in entries
        if entry.startswith(prefix)
        and entry.rstrip("/").count("/") == parent.count("/") + 1
        and (entry.endswith("/") or not dirs_only)
    }


def filetree_listed(root: Path) -> set[str]:
    listed = set(LISTED.findall((root / FILETREE).read_text(encoding="utf-8")))
    if len(listed) <= 50:
        raise RuntimeError(f"control: FILETREE.md lists only {len(listed)} paths")
    return listed


def filetree_missing_top_level(entries: set[str], listed: set[str]) -> list[str]:
    return sorted({entry for entry in entries if entry.rstrip("/").count("/") == 0} - listed)


def filetree_missing_mapped(entries: set[str], listed: set[str]) -> list[str]:
    expected: set[str] = set()
    for parent in COMPLETE_CHILDREN:
        expected |= _children(entries, parent, dirs_only=False)
    for parent in COMPLETE_SUBFOLDERS:
        expected |= _children(entries, parent, dirs_only=True)
    if "apps/webui/" not in expected:
        raise RuntimeError("control: apps/webui/ must be a mapped child")
    return sorted(expected - listed)


def filetree_stale(entries: set[str], listed: set[str]) -> list[str]:
    return sorted(path for path in listed if path not in entries)


# ----- CLI ----------------------------------------------------------------------


def problems(root: Path) -> list[str]:
    """One line per hygiene failure; empty when the tree is clean."""
    entries, listed = tree_entries(tracked_paths(root)), filetree_listed(root)
    checks = {
        "tracked file matches .gitignore (git rm --cached it)": new_tracked_ignored(root),
        f"stale line in {TRACKED_IGNORED_BASELINE} (delete it)": stale_tracked_ignored_baseline(root),
        "top-level entry missing from FILETREE.md": filetree_missing_top_level(entries, listed),
        "mapped entry missing from FILETREE.md": filetree_missing_mapped(entries, listed),
        "FILETREE.md lists an untracked path": filetree_stale(entries, listed),
    }
    return [f"{why}: {path}" for why, paths in checks.items() for path in paths]


def main() -> int:
    found = problems(REPO)
    for line in found:
        print(f"::error title=tree hygiene::{line}")
    print(f"[tree-hygiene] {len(found)} problem(s) (tracked-ignored, FILETREE.md)")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
