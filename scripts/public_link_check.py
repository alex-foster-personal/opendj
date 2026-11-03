"""Find relative links in PUBLIC markdown that would be broken on the public mirror.

The public mirror publishes an allowlist of paths (the recipe is private), so a
relative link can resolve in this repository and still 404 on the mirror. This
check reads the public path set from a file, one repo-relative path per line,
and reports every relative link in public markdown (repo root, ``docs/`` and
``open-dj/``) whose target is missing at ``--ref`` or is not public.

Produce the list from the live mirror:

    gh api 'repos/alex-foster-personal/opendj/git/trees/main?recursive=1' \
      --jq '.tree[] | select(.type == "blob") | .path' > public-paths.txt
    python -m scripts.public_link_check --public-list public-paths.txt

Exit 0 when no link is broken, 1 when any is, 2 when the inputs are unusable
(an empty or unreadable public list, a ref that does not resolve, git missing
from PATH, or no public markdown found).

Acceptance ((private path)):
- [if] a public doc links a file that exists but is not in the public list [then] it is reported
- [if] a public doc links a file that does not exist at the ref [then] it is reported
- [if] a link points at a public file, a public directory, a URL or an anchor [then] it is not reported
- [if] the public list is empty [then] the check exits 2 instead of reporting zero
"""

from __future__ import annotations

import argparse
import posixpath
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

PUBLIC_DOC_ROOTS = ("docs/", "open-dj/")
# A destination is either `<...>` (may hold spaces) or a bare run of non-space characters.
INLINE_LINK = re.compile(r"\]\(\s*(?:<([^>\n]+)>|([^)\s]+))(?:\s+\"[^\"]*\")?\s*\)")
# A footnote definition (`[^1]: text`) is not a link, so `[^` never starts a reference.
REFERENCE_LINK = re.compile(
    r"""^\s{0,3}\[(?!\^)[^\]]+\]:\s*(?:<([^>\n]+)>|(\S+))(?:\s+(?:"[^"]*"|'[^']*'|\([^)]*\)))?\s*$"""
)
FENCE = re.compile(r"^\s{0,3}(```|~~~)")
SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


@dataclass(frozen=True)
class BrokenLink:
    source: str
    line: int
    target: str
    reason: str


# ----- git ---------------------------------------------------------------------


class CannotMeasure(Exception):
    """An input the check cannot use: exit 2, never a verdict."""


def _git_executable() -> str:
    git = shutil.which("git")
    if git is None:
        raise CannotMeasure("git is not on PATH")
    return git


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        [_git_executable(), "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8", check=False
    )
    if result.returncode != 0:
        raise CannotMeasure(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _tree_paths(repo: Path, ref: str) -> set[str]:
    return set(_git(repo, "ls-tree", "-r", "--name-only", ref).splitlines())


def _read_at_ref(repo: Path, ref: str, path: str) -> str:
    return _git(repo, "show", f"{ref}:{path}")


# ----- classification ------------------------------------------------------------


def is_public_markdown(path: str) -> bool:
    """The measured scope: root, docs/ and open-dj/ markdown, the reader-facing docs.

    Public markdown elsewhere (for example under apps/) is deliberately out of scope,
    and the summary line names this scope so the denominator is never overstated.
    """
    if not path.endswith(".md"):
        return False
    return "/" not in path or path.startswith(PUBLIC_DOC_ROOTS)


def _dirs_of(paths: set[str]) -> set[str]:
    dirs: set[str] = set()
    for path in paths:
        parts = path.split("/")[:-1]
        for depth in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:depth]))
    return dirs


def extract_links(text: str) -> list[tuple[int, str]]:
    links: list[tuple[int, str]] = []
    in_fence = False
    for number, line in enumerate(text.splitlines(), start=1):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        links += [(number, m.group(1) or m.group(2)) for m in INLINE_LINK.finditer(line)]
        reference = REFERENCE_LINK.match(line)
        if reference:
            links.append((number, reference.group(1) or reference.group(2)))
    return links


def find_broken_links(sources: dict[str, str], tree: set[str], public: set[str]) -> list[BrokenLink]:
    tree_dirs, public_dirs = _dirs_of(tree), _dirs_of(public)
    broken: list[BrokenLink] = []
    for source, text in sorted(sources.items()):
        for line, target in extract_links(text):
            if SCHEME.match(target) or target.startswith(("#", "/")):
                continue
            path = target.split("#", 1)[0].split("?", 1)[0]
            if not path:
                continue
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(source), path))
            if resolved == ".." or resolved.startswith("../"):
                broken.append(BrokenLink(source, line, target, "escapes the repository"))
            elif resolved not in tree and resolved not in tree_dirs:
                broken.append(BrokenLink(source, line, target, "missing"))
            elif resolved not in public and resolved not in public_dirs:
                broken.append(BrokenLink(source, line, target, "not on the public mirror"))
    return broken


# ----- cli -----------------------------------------------------------------------


def _load_inputs(public_list: Path, repo: Path, ref: str) -> tuple[set[str], set[str], dict[str, str]]:
    public = {line.strip() for line in public_list.read_text(encoding="utf-8").splitlines() if line.strip()}
    if not public:
        raise CannotMeasure(f"public list {public_list} is empty")
    tree = _tree_paths(repo, ref)
    sources = {path: _read_at_ref(repo, ref, path) for path in sorted(tree & public) if is_public_markdown(path)}
    return public, tree, sources


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--public-list", type=Path, required=True)
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)

    try:
        public, tree, sources = _load_inputs(args.public_list, args.repo, args.ref)
    except (CannotMeasure, OSError) as exc:
        print(f"[ERROR] cannot measure: {exc}", file=sys.stderr)
        return 2
    if not sources:
        print("[ERROR] no public markdown found at the ref: cannot measure", file=sys.stderr)
        return 2

    broken = find_broken_links(sources, tree, public)
    for link in broken:
        print(f"{link.source}:{link.line}: {link.target} ({link.reason})")
    files = len({link.source for link in broken})
    print(
        f"[{'FAIL' if broken else 'OK'}] {len(broken)} broken link(s) in {files} of {len(sources)} "
        f"public markdown file(s) under root, docs/ and open-dj/ at {args.ref}"
    )
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
