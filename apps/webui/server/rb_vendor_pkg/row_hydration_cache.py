"""In-memory, mtime-revalidated caches for ANLZ-derived library row hydration.

Preview strips and PVDI vocal payloads are the same caching problem twice: both
are immutable per (source file, mtime), both are re-derived once per library row,
and the library pane re-walks every row every 60 s -- so the uncached form costs a
full ANLZ read plus a PMAI section walk per row per poll. One primitive serves
both (house rule D5: cache logic lives in one module, not once per call site).

Distinct from ``anlz_cache.py``, which is the on-disk, schema-versioned JSON cache
for whole ``/anlz`` payloads. This one is process-local and holds decoded fragments.

Validity is (source path, mtime): a re-analyzed track gets a new mtime and decodes
again on its next read, and a preview strip that starts resolving to a different
sibling (.2EX -> .EXT -> .DAT) is a miss rather than a stale hit.

Entries are never evicted. Each cache is bounded by the library's ANLZ file count
(~10k preview strips at ~500 B, ~4k PVDI carriers at a few hundred bytes of
regions), a few MB in total -- an eviction policy would cost more than it saves.
"""

from __future__ import annotations

import threading
from copy import deepcopy
from typing import Any, Generic, TypeVar

_V = TypeVar("_V")


class MtimeCache(Generic[_V]):
    """``key -> value``, invalidated when its source file's path or mtime moves.

    ``copy`` decides what a hit hands back. Leave it False when the value is
    immutable (a tuple of primitives); set it True when callers receive a mutable
    payload that must not be able to reach back into the cache -- row payloads go
    to serializers, and one caller mutating its row must not poison the next.
    """

    def __init__(self, *, copy: bool = False) -> None:
        self._copy: bool = copy
        self._lock: threading.Lock = threading.Lock()
        self._entries: dict[str, tuple[str, float, _V]] = {}

    def get(self, key: str, source: str, mtime: float) -> _V | None:
        """The cached value for ``key``, or None when it is absent or stale."""
        with self._lock:
            hit = self._entries.get(key)
        if hit is None or hit[0] != source or hit[1] != mtime:
            return None
        return deepcopy(hit[2]) if self._copy else hit[2]

    def put(self, key: str, source: str, mtime: float, value: _V) -> _V:
        """Store ``value`` under ``key``, and return what this caller should use."""
        with self._lock:
            self._entries[key] = (source, mtime, value)
        return deepcopy(value) if self._copy else value

    def clear(self) -> None:
        """Drop every entry. Tests call this to force a cold read."""
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


# Keyed by AnalysisDataPath; the source is whichever sibling the preview fallback
# chain resolved to, so a switch between .2EX/.EXT/.DAT invalidates on its own.
_PREVIEW_CACHE: MtimeCache[tuple[str, int]] = MtimeCache()

# Keyed by .2EX path, which is also its own source file. Copies on every hit: the
# payload is a dict of region dicts handed straight to the row serializers.
_VOCALS_CACHE: MtimeCache[dict[str, Any]] = MtimeCache(copy=True)
