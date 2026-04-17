"""MP3 retention: prune old audio segments, keep timeline.jsonl.

Plan 12-01 Step 7. Retention default is 90 days (Open Question 4).
Only ``audio_*.mp3`` files are candidates; ``timeline.jsonl`` and
``manifest.json`` are NEVER pruned (those are tiny and the whole
point of the recorder).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from . import paths as sets_paths


@dataclass
class PruneCandidate:
    path: Path
    size_bytes: int
    age_days: float


def find_candidates(
    retention_days: int = 90,
    *,
    root: Path | None = None,
    now: float | None = None,
) -> list[PruneCandidate]:
    """Return MP3 segments older than ``retention_days``.

    Scans ``<root>/<session>/audio_*.mp3`` (``root`` defaults to
    :data:`apps.sets.paths.SETS_DIR`). ``now`` lets tests pin the clock.
    """
    base = Path(root) if root is not None else sets_paths.SETS_DIR
    wall_now = now if now is not None else time.time()
    cutoff = wall_now - retention_days * 86400.0
    out: list[PruneCandidate] = []
    if not base.exists():
        return out
    for session_dir in sorted(base.iterdir()):
        if not session_dir.is_dir():
            continue
        for mp3 in sorted(session_dir.glob("audio_*.mp3")):
            mtime = mp3.stat().st_mtime
            if mtime < cutoff:
                age = max(0.0, (wall_now - mtime) / 86400.0)
                out.append(
                    PruneCandidate(
                        path=mp3,
                        size_bytes=mp3.stat().st_size,
                        age_days=age,
                    )
                )
    return out


def prune(
    retention_days: int = 90,
    *,
    dry_run: bool = True,
    root: Path | None = None,
    now: float | None = None,
) -> list[PruneCandidate]:
    """Delete MP3s older than ``retention_days`` unless ``dry_run``.

    Returns the list of candidates inspected (deleted or would-delete).
    """
    candidates = find_candidates(retention_days, root=root, now=now)
    if not dry_run:
        for c in candidates:
            c.path.unlink(missing_ok=True)
    return candidates


__all__ = ["PruneCandidate", "find_candidates", "prune"]
