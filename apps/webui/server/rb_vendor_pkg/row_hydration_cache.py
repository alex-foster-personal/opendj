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

import os
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Generic, TypeVar

from apps.shared.platform_paths import AssetResolver, MappedPath, PathMap

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


# ----- stat-witnessed row cache (LIBM-137) ------------------------------------
#
# The two caches above still pay for finding their source file: every hit comes
# after a share-root containment walk (about six opens per asset path), three or
# four of them per row. Measured Thu 1 Oct 2026 on a 9,713-track library: 8,820
# opens and 8,283 stats per 500 warm rows, 380 ms of a 590 ms listing page.
#
# This one is keyed on the vendor's own path strings, so a hit needs no walk. It
# is NOT the cross-request containment cache that was rejected on review (see
# ``AssetResolver``): that one kept a "safe" verdict and the resolved path, and a
# later request opened the path without rerunning the check. Here a hit opens
# nothing and hands back no path. It returns data that was derived, earlier, from
# files the containment walk approved at that time, and only while every path the
# derivation looked at still names the same inode with the same size and mtime.
# Swap a directory for a symlink to somewhere else and the witnesses stop
# matching, which sends the row back through the full walk, where it is refused.
#
# There is no time dimension: nothing expires, and nothing is served on age.

#: ``(st_mode, st_ino, st_dev, st_size, st_mtime_ns)`` of a path, None when absent.
Witness = tuple[int, int, int, int, int] | None


def witness_of(path: str) -> Witness:
    """What ``path`` names right now, without following a final symlink."""
    try:
        st = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    return (st.st_mode, st.st_ino, st.st_dev, st.st_size, st.st_mtime_ns)


class WitnessingResolver(AssetResolver):
    """Records what each path named at the moment it was resolved.

    Wraps the per-call :class:`AssetResolver`, so rows that share a path still
    share one containment walk. The witness is taken BEFORE the caller reads the
    file: a change after that point shows as a mismatch on the next request
    instead of being cached under the new state.

    ``cacheable`` goes False when any path was refused or is not share-relative.
    A refused path has no witness to revalidate, and only a share-relative path
    maps without consulting the filesystem or the path map, so only that mapping
    is safe to key on its input string.
    """

    def __init__(self, inner: AssetResolver) -> None:
        super().__init__()
        self._inner: AssetResolver = inner
        self.witnesses: dict[str, Witness] = {}
        self.cacheable: bool = True

    def resolve_asset_path(
        self, asset_path: str, *, path_map: PathMap | None = None
    ) -> MappedPath:
        return self._record(self._inner.resolve_asset_path(asset_path, path_map=path_map))

    def resolve_asset_sibling(self, mapped: MappedPath, candidate: Path) -> MappedPath:
        return self._record(self._inner.resolve_asset_sibling(mapped, candidate))

    def witness(self, path: Path) -> None:
        """Witness a path the caller is about to probe without resolving it."""
        self.witnesses.setdefault(str(path), witness_of(str(path)))

    def _record(self, mapped: MappedPath) -> MappedPath:
        if mapped.resolved is None or mapped.reason != "share":
            self.cacheable = False
        else:
            self.witness(mapped.resolved)
        return mapped


class StatWitnessCache(Generic[_V]):
    """``key -> value``, valid while every witnessed path is unchanged on disk."""

    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        self._entries: dict[tuple[str, ...], tuple[tuple[tuple[str, Witness], ...], _V]] = {}

    def get(self, key: tuple[str, ...]) -> _V | None:
        with self._lock:
            hit = self._entries.get(key)
        if hit is None:
            return None
        witnesses, value = hit
        for path, witness in witnesses:
            if witness_of(path) != witness:
                return None
        return value

    def put(self, key: tuple[str, ...], resolver: WitnessingResolver, value: _V) -> None:
        """Store ``value`` unless the resolver saw a path it cannot witness."""
        if not resolver.cacheable:
            return
        with self._lock:
            self._entries[key] = (tuple(resolver.witnesses.items()), value)

    def clear(self) -> None:
        """Drop every entry. Tests call this to force a cold read."""
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
