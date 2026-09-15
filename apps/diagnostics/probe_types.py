"""Primitives shared by every OpenDJ diagnostics probe module.

INTERPRETER FLOOR: like the rest of this package, this module runs under
``/usr/bin/python3`` (3.9 on current macOS) and is stdlib-only. No 3.10+/3.11+
runtime syntax; annotations are exempt because they are deferred.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MIB = 1024 * 1024
DEFAULT_INTERVAL_SECONDS = 15.0
DEFAULT_DEEP_INTERVAL_SECONDS = 300.0
DEFAULT_LOG_CAP_BYTES = 20 * MIB
DEFAULT_OUTPUT_DIR = Path.home() / ".local/share/music-dj-tools/performance"
# macOS derives the WebKit storage path from the bundle identifier, so the
# perf ring moves when the identifier does. The bake-off ended Sat 29 Aug 2026
# (OPS-08): the plain product is now the default and the lane-b build is what
# is still installed on machines that have not taken a new dmg. Both are tried,
# newest-write-wins, the same candidate-list shape as virgin_boot.sh.
DEFAULT_BUNDLE_IDS = ("com.opendj.desktop", "com.opendj.desktop.lane-b")


class ProbeUnavailable(RuntimeError):
    """The requested native metric is unavailable on this platform."""


@dataclass(frozen=True)
class ProcessRow:
    pid: int
    ppid: int
    pgid: int
    command: str


def run_text(command: list[str], timeout: float = 4.0) -> str:
    return subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    ).stdout


def round_mb(value: int) -> float:
    return round(value / MIB, 1)


def parse_byte_count(raw: str) -> int | None:
    match = re.fullmatch(r"([0-9.]+)([KMGTP])?", raw.strip(), re.IGNORECASE)
    if not match:
        return None
    value = float(match.group(1))
    unit = (match.group(2) or "").upper()
    scale = {"": 1, "K": 1024, "M": MIB, "G": 1024 * MIB, "T": 1024**4, "P": 1024**5}
    return int(value * scale[unit])


def utc_now() -> str:
    # datetime.UTC is 3.11; this package runs on /usr/bin/python3 (3.9).
    now = datetime.now(timezone.utc)  # noqa: UP017
    return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
