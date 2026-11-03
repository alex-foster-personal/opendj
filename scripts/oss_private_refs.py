"""Find references a published tree makes to private paths it does not ship.

The publish recipe keeps files by allowlist, so `specs/`, `app_docs/`, `docs/threads/`
and most of `.planning/` never reach the mirror, yet kept files still name them
(`see (private path)`). Each such reference is a dead link that also
advertises the private record's name. A reference is PRIVATE when it starts with a
withheld root and the published tree tracks neither that file nor a directory
under it, so the two `.planning/` files the mirror does keep stay untouched.

    python -m scripts.oss_private_refs check --root BUILD   # exit 1 on any private ref

This module is the fail-closed publication gate only. Fix offending references at
source (or in the private recipe) once; do not grow a rewriter grammar here. Check
may over-report rather than miss.

A reference may lead with `./` or `../` segments or a root `/` (Markdown links),
may use Windows `\\` separators, and may be a glob
(`.planning/**` in pyproject.toml or a CI paths filter). Relative leads are read as
repo-root relative. A glob counts as published when it matches a file the build
tracks. Trailing slashes on directory refs are normalized before the published check.

Only regular, strictly UTF-8, NUL-free files are scanned. Symlinks are never
followed; their link text is still checked, so a private link fails closed.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

WITHHELD_ROOTS: tuple[str, ...] = ("specs", "app_docs", "docs/threads", ".planning")
_SEP = r"[/\\]"
_LEAD = r"(?:/|(?:\.\.?" + _SEP + r")*)"
_ROOTS = r"(?:" + "|".join(re.escape(r).replace("/", _SEP) for r in WITHHELD_ROOTS) + r")" + _SEP
# Fail-closed finder: take a path-like token after a withheld root. Include braces,
# brackets, parens, escapes and glob chars so check finds them; stop at whitespace
# and a few hard delimiters. Trailing closers are trimmed when classifying.
_BODY = r"[^\s<>\"'`|#]+"
# Empty when a reference names a withheld root itself (`see specs/`).
_OPT_BODY = r"(?:" + _BODY + r")?"
_PERCENT = re.compile(r"%[0-9A-Fa-f]{2}")
_REF = re.compile(
    r"(?<=<)(?P<alead>"
    + _LEAD
    + r")(?P<aref>"
    + _ROOTS
    + r"[^<>\n]*[^<>\s])(?=>)"
    # A repository URL (`.../blob/main/specs/x.md`) names the path after the ref. A
    # branch ref may hold slashes (`feature/topic`), so skip one or more segments.
    + r"|/(?:blob|tree|raw|edit)/(?:[^/\s]+/)+?(?P<uref>"
    + _ROOTS
    + _OPT_BODY
    + r")"
    + r"|(?<![\w./\\-])(?P<lead>"
    + _LEAD
    + r")(?P<ref>"
    + _ROOTS
    + _OPT_BODY
    + r")"
)
_UNESCAPE = re.compile(r"\\([^\w\s/\\])")
_GLOB = re.compile(r"[*?\[]")
# Markdown / prose closers that wrap a path; not part of the repo path itself.
_TRAIL = ".,:;)]}>\"'"
_OPENERS = {")": "(", "]": "[", "}": "{"}
_ENTITY = re.compile(
    r"&(?:#([0-9]{1,7})|#x([0-9A-Fa-f]{1,6})|(sol|bsol|amp));",
    re.IGNORECASE,
)
_NAMED = {"sol": "/", "bsol": "\\", "amp": "&"}


def _glob_match(path: str, pattern: str) -> bool:
    """A glob as a regex. `**` crosses directories, `*` and `?` do not.

    Written out rather than delegated to `fnmatch`, whose `*` crosses `/`.
    """
    table = {"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*", "?": "[^/]"}
    parts = re.split(r"(\*\*/|\*\*|\*|\?)", pattern)
    return re.fullmatch("".join(table.get(part, re.escape(part)) for part in parts), path) is not None


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    ref: str


def _published(ref: str, tracked: frozenset[str]) -> bool:
    # `?` may start a URL query (`?raw`, `?raw=1`) or be a glob: a link whose
    # path before the `?` ships is published either way.
    if "?" in ref and _published(ref.split("?", 1)[0], tracked):
        return True
    if _GLOB.search(ref):
        return any(_glob_match(p, ref) for p in tracked)
    return ref in tracked or any(p.startswith(ref + "/") for p in tracked)


def _ref(m: re.Match[str]) -> str:
    """Repo path a match names: escapes undone, separators normalized, closers trimmed."""
    raw = next(m[g] for g in ("aref", "uref", "ref") if m[g] is not None)
    s = unquote(_UNESCAPE.sub(r"\1", raw).replace("\\", "/")).rstrip("/")
    while len(s) > 1 and s[-1] in _TRAIL and not _balanced_closer(s):
        s = s[:-1]
    return s.rstrip("/")


def _balanced_closer(s: str) -> bool:
    """A final `)`, `]` or `}` that closes a group opened in the path belongs to it."""
    opener = _OPENERS.get(s[-1])
    return opener is not None and s.count(opener) >= s.count(s[-1])


def _decoded_ascii(m: re.Match[str]) -> str:
    """One percent escape undone, unless whitespace: `%20` stays inside its path token."""
    char = chr(int(m.group()[1:], 16))
    return char if char.isascii() and not char.isspace() else m.group()


def _decoded_entity(match: re.Match[str]) -> str:
    decimal, hexa, named = match.group(1), match.group(2), match.group(3)
    try:
        if decimal is not None:
            char = chr(int(decimal))
        elif hexa is not None:
            char = chr(int(hexa, 16))
        else:
            char = _NAMED[named.lower()]
    except (ValueError, OverflowError):
        return match.group()
    if char.isascii() and char.isprintable() and not char.isspace():
        return char
    return match.group()


def _decoded_view(text: str) -> str:
    current = text
    while True:
        nxt = _ENTITY.sub(_decoded_entity, _PERCENT.sub(_decoded_ascii, current))
        if nxt == current:
            return current
        current = nxt


def private_refs(text: str, tracked: frozenset[str]) -> list[re.Match[str]]:
    # An encoded root (`specs%2Fx.md`, `%73pecs/x.md`) must reach the same matcher as a plain one.
    view = _decoded_view(text)
    return [m for m in _REF.finditer(view) if not _published(_ref(m), tracked)]


def _git() -> str:
    if not (git := shutil.which("git")):
        raise SystemExit("oss_private_refs: git not found on PATH")
    return git


def tracked_files(root: Path) -> frozenset[str]:
    out = subprocess.run([_git(), "-C", str(root), "ls-files", "-z"], check=True, capture_output=True).stdout
    return frozenset(p for p in out.decode().split("\0") if p)


def _text(raw: bytes) -> str | None:
    if b"\0" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _texts(root: Path, tracked: Iterable[str]) -> Iterable[tuple[str, str]]:
    """Yield (rel, text) for symlink targets and regular UTF-8 files inside root."""
    real_root = root.resolve()
    for rel in sorted(tracked):
        path = root / rel
        if path.is_symlink():
            yield rel, os.readlink(path)
            continue
        inside = path.is_file() and path.resolve().is_relative_to(real_root)
        if inside and (text := _text(path.read_bytes())) is not None:
            yield rel, text


def check_tree(root: Path) -> tuple[int, list[Finding]]:
    tracked = tracked_files(root)
    scanned, found = 0, []
    for rel, text in _texts(root, tracked):
        scanned += 1
        found.extend(Finding(rel, m.string.count("\n", 0, m.start()) + 1, m.group()) for m in private_refs(text, tracked))
    return scanned, found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mode", choices=("check",), help="fail closed on any private ref")
    ap.add_argument("--root", type=Path, required=True, help="published build checkout")
    args = ap.parse_args(argv)
    scanned, found = check_tree(args.root)
    for f in found:
        print(f"{f.path}:{f.line}: {f.ref}")
    print(f"oss_private_refs: scanned={scanned} findings={len(found)}")
    return 1 if found or not scanned else 0


if __name__ == "__main__":
    sys.exit(main())
