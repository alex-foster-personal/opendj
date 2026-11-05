"""Process identity for test helpers: a pid's start time, so a reused pid never reads as
the process a helper spawned.

Kept here rather than imported from ``scripts/server_owner_guard.py`` because a global
helper that imports a ``scripts`` module carries the whole ``scripts`` closure into every
plan (ADR-NEW-layering-edges-and-contracts). The guard runs as a bare script and cannot
import from ``tests``; ``tests/scripts/test_server_owner_guard.py`` holds the two equal.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def process_start_time(pid: int) -> str | None:
    """Identity of a pid: its start time. None when the pid is gone or a zombie."""
    if sys.platform.startswith("linux"):
        try:
            text = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return None
        fields = text[text.rfind(")") + 2 :].split()
        return None if fields[0] == "Z" else fields[19]
    out = subprocess.run(
        ["ps", "-o", "stat=,lstart=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "LC_ALL": "C"},
    ).stdout.strip()
    if not out or out.startswith("Z"):
        return None
    return out.partition(" ")[2].strip()
