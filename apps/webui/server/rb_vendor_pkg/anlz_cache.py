"""ANLZ JSON file cache: schema-versioned, mtime-keyed, atomic tempfile writes.

Moved verbatim from ``apps/webui/server/rb_vendor.py`` C7 (lines 1341-1414 on
``af--t4-design``), per ``.planning/t3b-decomposition-map.md`` target #13
(``store/caches/anlz_cache.py``, budget 100 lines). ``rb_vendor.py``
re-exports these five names, so the pinning test suite
(``tests/webui/test_rb_vendor_units.py`` interrupted-write /
concurrent-writer / concurrent-reader cases,
``tests/webui/test_rb_vendor_cache.py``) sees no behavior change.

``ANLZ_CACHE_DIR`` and ``ANLZ_CACHE_SCHEMA`` are read off the ``config``
module at call time, never imported by value: they are rebindable overrides
that ``.planning/e2e-gating/run_daemon.py`` and ``test_rb_vendor_units.py``
assign to. Wave 4 moved them (and ``not_found``) out of ``rb_vendor`` into
``apps/adapters/rekordbox/``, so those assignments now target ``config``;
a by-value import here would freeze whichever value was current at import.

This module cannot follow them out of ``apps.webui`` yet -- not because of
anything it imports, but because its only caller that still lives here,
``anlz.py``, reaches ``beatgrid_diagnostics``. See ``rb_vendor.py``.

The per-path write lock (``_ANLZ_CACHE_LOCKS_GUARD`` / ``_ANLZ_CACHE_LOCKS``)
is duplicated here rather than shared with ``beatgrid_issue_cache.py``,
which used the SAME dict in rb_vendor.py before the split (rb_vendor.py:1352
``_cache_lock`` was called from both C7 and C8). This is safe: the dict is
keyed by full absolute ``Path``, and ``ANLZ_CACHE_DIR`` and
``BEATGRID_ISSUE_CACHE_DIR`` never produce overlapping paths, so two
independent lock dicts serialize the exact same set of concurrent writers
per path as the one shared dict did. Not called out as a shared dependency
in the decomposition map's cluster inventory (C7/C8 responsibility table) --
flagged here as map drift for whoever lands S8.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from apps.adapters.rekordbox import config
from apps.adapters.rekordbox.errors import not_found

log = logging.getLogger(__name__)

# Windows does not allow os.replace() while another thread has the destination
# open. Stage writes concurrently, then serialize reads and atomic publication
# per cache entry so unrelated tracks remain independent.
_ANLZ_CACHE_LOCKS_GUARD = threading.Lock()
_ANLZ_CACHE_LOCKS: dict[Path, threading.Lock] = {}


def _anlz_mtime(directory: Path) -> float:
    files = sorted(directory.glob("ANLZ*"))
    if not files:
        raise not_found(
            "ANALYSIS_NOT_FOUND", f"no ANLZ files in directory {directory}"
        )
    return max(f.stat().st_mtime for f in files)


def _cache_path(stable_id: str) -> Path:
    return config.ANLZ_CACHE_DIR / f"{stable_id}.json"


def _cache_lock(path: Path) -> threading.Lock:
    with _ANLZ_CACHE_LOCKS_GUARD:
        lock = _ANLZ_CACHE_LOCKS.get(path)
        if lock is None:
            lock = threading.Lock()
            _ANLZ_CACHE_LOCKS[path] = lock
        return lock


def _load_cached_payload(
    stable_id: str, anlz_mtime: float, points: int
) -> dict[str, Any] | None:
    path = _cache_path(stable_id)
    with _cache_lock(path):
        if not path.is_file():
            return None
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("anlz cache unreadable, recomputing: %s (%s)", path, exc)
            return None
    if (
        cached.get("schema") == config.ANLZ_CACHE_SCHEMA
        and cached.get("anlz_mtime") == anlz_mtime
        and cached.get("points") == points
    ):
        return cached["payload"]
    return None


def _store_cached_payload(
    stable_id: str, anlz_mtime: float, points: int, payload: dict[str, Any]
) -> None:
    """Persist a cache entry atomically through a unique sibling tempfile.

    A crash mid-write must never leave a truncated {stable_id}.json behind:
    the entry lands in a unique sibling tempfile first and only ``os.replace``
    publishes it, so concurrent readers and writers see a complete entry.
    """
    config.ANLZ_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(stable_id)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_text(
            json.dumps(
                {
                    "schema": config.ANLZ_CACHE_SCHEMA,
                    "anlz_mtime": anlz_mtime,
                    "points": points,
                    "payload": payload,
                }
            ),
            encoding="utf-8",
        )
        with _cache_lock(path):
            os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
