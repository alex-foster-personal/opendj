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

The two mtime caches below are never evicted. Each is bounded by the library's ANLZ file count
(~10k preview strips at ~500 B, ~4k PVDI carriers at a few hundred bytes of
regions), a few MB in total -- an eviction policy would cost more than it saves.
"""

from __future__ import annotations

import hashlib
import os
import stat
import struct
import threading
from collections import OrderedDict
from copy import deepcopy
from typing import Any, Generic, TypeVar

from apps.shared.fd_anchored_walk import identity_of

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


# ===== the listing's row asset cache (LIBM-137; row_assets.py fills it) =====

_WITNESS = struct.Struct("<6Q")
_U64: int = (1 << 64) - 1
#: Witness of a path that names nothing.
_ABSENT: bytes = bytes(_WITNESS.size)

# ----- witnesses -------------------------------------------------------------


def file_witness(st: os.stat_result) -> bytes:
    """All six identity fields of a file, packed."""
    return _WITNESS.pack(*(value & _U64 for value in identity_of(st)))


def directory_witness(st: os.stat_result) -> bytes:
    """Type and inode of a directory. Its size and times move with its contents."""
    return _WITNESS.pack(stat.S_IFMT(st.st_mode), st.st_ino & _U64, st.st_dev & _U64, 0, 0, 0)


def digest(witnesses: list[bytes]) -> bytes:
    """What a row's paths looked like, as the 16 bytes an entry keeps."""
    return hashlib.blake2b(b"".join(witnesses), digest_size=16).digest()


def lstat_below(root_fd: int, relative: str) -> os.stat_result | OSError | None:
    """``lstat`` below the anchored root: None for a path that names nothing,
    the error itself for anything else."""
    try:
        return os.stat(relative, dir_fd=root_fd, follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        # A symlink loop, an unreadable directory, a volume that went away: the
        # entry cannot be confirmed, so it must not be served (and the page
        # must not fail). The row goes back through the walk.
        return exc



#: (preview_b64, preview_max, artwork_available, artwork_status, vocals JSON).
RowValue = tuple[str | None, int | None, bool | None, str, str]
RootKey = tuple[str, int, int]


class RowAssetCache:
    """``(AnalysisDataPath, ImagePath) -> (witnesses, value)`` for ONE share root.

    Bound to the root it was filled under, by path string AND by the device
    and inode that path opened to: the same vendor strings name different
    files under another library, so a different root empties the cache.
    Least recently used, at most ``max_entries`` rows.
    """

    def __init__(self, max_entries: int) -> None:
        self._max_entries: int = max_entries
        self._lock: threading.Lock = threading.Lock()
        self._root: RootKey | None = None
        self._entries: OrderedDict[tuple[str, str], tuple[bytes, RowValue]] = OrderedDict()

    def get(self, root: RootKey, key: tuple[str, str]) -> tuple[bytes, RowValue] | None:
        with self._lock:
            if root != self._root:
                return None
            hit = self._entries.get(key)
            if hit is not None:
                self._entries.move_to_end(key)
            return hit

    def put(self, root: RootKey, key: tuple[str, str], witnesses: bytes, value: RowValue) -> None:
        with self._lock:
            if root != self._root:
                self._entries.clear()
                self._root = root
            self._entries[key] = (witnesses, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def discard(self, root: RootKey, key: tuple[str, str]) -> None:
        with self._lock:
            if root == self._root:
                self._entries.pop(key, None)

    def clear(self) -> None:
        """Drop every entry: a library switch, or a test forcing a cold read."""
        with self._lock:
            self._entries.clear()
            self._root = None

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
