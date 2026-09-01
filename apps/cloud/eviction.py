"""LRU cache eviction for the ADR 06 cache tier.

Split out of :mod:`apps.cloud.hydration` (round 2 hardening, the file-size
review gate) so ``hydration.py`` stays under the 600-line threshold in
``scripts/quality_gate.py``. :class:`HydrationError` lives here rather than
in ``hydration.py`` because :func:`evict_cache` is the one piece of that
module with no dependency on the rest of the read/write path; putting the
shared exception on this side and having ``hydration.py`` import it back
avoids a circular import between the two modules. ``hydration`` re-exports
everything here, so existing callers that do ``hydration.evict_cache`` or
``hydration.HydrationError`` are unaffected.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

BYTES_PER_MB: int = 1024 * 1024


class HydrationError(RuntimeError):
    """Raised when policy resolution or hydration cannot proceed."""


@dataclass(frozen=True)
class EvictionResult:
    """What :func:`evict_cache` removed and what it left behind."""

    evicted: tuple[Path, ...]
    bytes_freed: int
    bytes_remaining: int
    budget_bytes: int


def evict_cache(cache_dir: Path, budget_mb: int) -> EvictionResult:
    """Trim ``cache_dir`` to ``budget_mb``, evicting least-recently-used first.

    Ordering is ``(atime, relative posix path)``. The path tiebreak is what
    makes a run reproducible: two files written in the same clock tick would
    otherwise be evicted in filesystem-listing order, which differs per
    machine and makes an eviction bug impossible to reproduce.

    A cache loss is an inconvenience, never data loss (ADR 06 point 2), so
    this deletes files without consulting the DB. It never removes
    directories, and a missing ``cache_dir`` is an empty cache, not an error.
    """
    if budget_mb < 0:
        raise HydrationError(f"budget_mb must be >= 0; got {budget_mb}")
    root = Path(cache_dir)
    budget_bytes = budget_mb * BYTES_PER_MB
    if not root.is_dir():
        return EvictionResult(
            evicted=(),
            bytes_freed=0,
            bytes_remaining=0,
            budget_bytes=budget_bytes,
        )

    entries: list[tuple[float, str, Path, int]] = []
    total = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        stat_result = path.stat()
        total += stat_result.st_size
        entries.append(
            (
                stat_result.st_atime,
                path.relative_to(root).as_posix(),
                path,
                stat_result.st_size,
            )
        )

    entries.sort(key=lambda item: (item[0], item[1]))
    evicted: list[Path] = []
    freed = 0
    for _atime, _rel, path, size in entries:
        if total - freed <= budget_bytes:
            break
        path.unlink()
        evicted.append(path)
        freed += size
    return EvictionResult(
        evicted=tuple(evicted),
        bytes_freed=freed,
        bytes_remaining=total - freed,
        budget_bytes=budget_bytes,
    )


__all__ = [
    "BYTES_PER_MB",
    "EvictionResult",
    "HydrationError",
    "evict_cache",
]
