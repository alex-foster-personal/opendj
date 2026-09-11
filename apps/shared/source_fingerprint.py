"""Content fingerprint for a live SQLite source file.

Path equality alone cannot prove a verdict was computed against THIS file:
the file at that path can be replaced in place (a MIK library upgrade, a
restored backup, a different machine's copy at a coincidentally identical
path) while the path itself never changes. ``size`` + ``mtime`` is the same
cheap identity check git, make and rsync use for "did this file change", and
it is fast enough to run on every ``apps.mik load`` invocation -- hashing a
multi-GB SQLite database on every CLI call would not be.

Shared by the producer (``apps.equivalence.verdict``, which stamps a
fingerprint into ``meta.sources`` at verdict time) and the consumer
(``apps.shared.equivalence.EquivalenceGate``, which checks the LIVE source
against that stamp before trusting the verdict) so the two sides can never
drift into different notions of "the same file".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def fingerprint(path: Path) -> dict[str, Any]:
    """``{path, exists, size, mtime}`` for ``path`` right now."""
    try:
        stat = path.stat()
    except OSError:
        return {"path": str(path), "exists": False}
    return {
        "path": str(path),
        "exists": True,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
    }


def matches(declared: Any, path: Path) -> bool:
    """True if ``path`` on disk still matches a previously recorded fingerprint.

    ``declared`` that is not a ``{size, mtime}``-carrying object -- a bare
    path string (the old, pre-fingerprint shape) or a malformed entry -- is
    NOT verifiable and this returns ``False`` rather than guessing a match:
    "cannot check" and "checked and it matches" must never look the same to
    a caller deciding whether to trust a verdict.
    """
    if not isinstance(declared, dict):
        return False
    if "size" not in declared or "mtime" not in declared:
        return False
    current = fingerprint(path)
    return (
        current["exists"]
        and declared.get("exists", True)
        and current["size"] == declared["size"]
        and current["mtime"] == declared["mtime"]
    )


__all__ = ["fingerprint", "matches"]
