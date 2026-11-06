"""Comment-stripped path lists used by CI always-run readers."""

from __future__ import annotations

from pathlib import Path


def read_commented_list(path: Path) -> list[str]:
    """Non-empty entries of a `#`-annotated path list; `#` starts a comment."""
    entries = (line.split("#", 1)[0].strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [entry for entry in entries if entry]
