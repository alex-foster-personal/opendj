"""Beatgrid-issue sidecar cache (browser Err column diagnostic).

Moved verbatim from ``apps/webui/server/rb_vendor.py`` C8 (lines 1417-1510 on
``af--t4-design``), per ``.planning/t3b-decomposition-map.md`` target #14
(``store/caches/beatgrid_issue_cache.py``, budget 120 lines). rb_vendor.py
re-exports ``cached_beatgrid_issue`` (public, in ``__all__``) plus the four
private helpers so ``build_anlz_payload`` (C9, still in rb_vendor.py -- S1's
slice) sees no behavior change.

Perf tradeoff (unchanged from the original comment): the diagnostic is only
ever COMPUTED here, piggybacked on a full ANLZ parse that already happened
for another reason (deck load, waveform view, RunAnalyses). Bulk row
listings (build_track_rows) and the browser's lazy per-row rb-meta fetch
never parse PQTZ themselves - rb-meta only READS the tiny cached verdict via
``cached_beatgrid_issue()``, so a track shows no Err dot until its full ANLZ
has actually been fetched once. That is an honest "not yet evaluated" state,
not a wrong answer, and it self-heals the first time anything reads that
track's /anlz.

Config constants (``BEATGRID_ISSUE_CACHE_DIR``, ``BEATGRID_ISSUE_CACHE_SCHEMA``)
and ``resolve_asset_path`` (C1) are looked up as ``rb_vendor.<name>`` inside
function bodies rather than imported by value -- see ``rb_vendor_pkg/db.py``'s
module docstring for the full rationale (module-level rebindable attributes,
monkeypatch safety, circular-import safety).

The per-path write lock is a private duplicate of the pattern in
``anlz_cache.py`` (which served both C7 and C8 through one shared dict
before the split); see that module's docstring for why splitting it is
behavior-preserving. ``beatgrid_diagnostics`` is imported directly (not via
``rb_vendor.beatgrid_diagnostics``) because nothing monkeypatches it on the
rb_vendor module -- it is only ever used, never stubbed, in the current test
suite.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from apps.webui.server import beatgrid_diagnostics, rb_vendor

log = logging.getLogger(__name__)

_BEATGRID_ISSUE_CACHE_LOCKS_GUARD = threading.Lock()
_BEATGRID_ISSUE_CACHE_LOCKS: dict[Path, threading.Lock] = {}


def _cache_lock(path: Path) -> threading.Lock:
    with _BEATGRID_ISSUE_CACHE_LOCKS_GUARD:
        lock = _BEATGRID_ISSUE_CACHE_LOCKS.get(path)
        if lock is None:
            lock = threading.Lock()
            _BEATGRID_ISSUE_CACHE_LOCKS[path] = lock
        return lock


def _beatgrid_issue_cache_path(stable_id: str) -> Path:
    return rb_vendor.BEATGRID_ISSUE_CACHE_DIR / f"{stable_id}.json"


def _read_beatgrid_issue_cache_entry(stable_id: str) -> dict[str, Any] | None:
    path = _beatgrid_issue_cache_path(stable_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("beatgrid-issue cache unreadable, ignoring: %s (%s)", path, exc)
        return None


def _store_beatgrid_issue_cache(
    stable_id: str, dat_mtime: float, issue: dict[str, Any] | None
) -> None:
    """Atomic write, same tempfile-then-replace pattern as _store_cached_payload."""
    rb_vendor.BEATGRID_ISSUE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _beatgrid_issue_cache_path(stable_id)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_text(
            json.dumps(
                {
                    "schema": rb_vendor.BEATGRID_ISSUE_CACHE_SCHEMA,
                    "dat_mtime": dat_mtime,
                    "issue": issue,
                }
            ),
            encoding="utf-8",
        )
        with _cache_lock(path):
            os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _ensure_beatgrid_issue_cached(
    stable_id: str, dat_mtime: float, beats: Sequence[Mapping[str, Any]]
) -> None:
    """Compute + persist the diagnostic only if the sidecar is missing or
    stale for this exact AnalysisDataPath mtime; a fresh match is a no-op so
    a warm anlz-cache hit stays cheap on every subsequent /anlz call."""
    entry = _read_beatgrid_issue_cache_entry(stable_id)
    if (
        entry is not None
        and entry.get("schema") == rb_vendor.BEATGRID_ISSUE_CACHE_SCHEMA
        and entry.get("dat_mtime") == dat_mtime
    ):
        return
    issue = beatgrid_diagnostics.detect_beatgrid_issue(beats)
    _store_beatgrid_issue_cache(stable_id, dat_mtime, issue)


def cached_beatgrid_issue(content: rb_vendor.RbContent) -> dict[str, Any] | None:
    """Cheap read-only lookup for GET /rb-meta - see the module comment above
    for the full perf rationale. None means either "no issue" or "never
    evaluated yet"; both are honest and this never fabricates a verdict."""
    if content.analysis_data_path is None:
        return None
    mapped = rb_vendor.resolve_asset_path(content.analysis_data_path)
    if mapped.resolved is None:
        return None
    try:
        dat_mtime = mapped.resolved.stat().st_mtime
    except OSError:
        return None
    entry = _read_beatgrid_issue_cache_entry(content.stable_id)
    if (
        entry is None
        or entry.get("schema") != rb_vendor.BEATGRID_ISSUE_CACHE_SCHEMA
        or entry.get("dat_mtime") != dat_mtime
    ):
        return None
    return entry.get("issue")
