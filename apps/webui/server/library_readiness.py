"""Per-present-track library readiness (READY-01, issue #1716).

``GET /api/v1/library/readiness`` answers "which present tracks are not
ready, and why" from stored artifacts: beatgrid, waveform preview, stems
(ready / missing / corrupt), analysis row/version, and a load-time
``sync_compatible`` verdict. Counts use the ``present`` denominator
(materialized local audio), never all ``tracks`` rows.

No FastAPI imports: the library router is the HTTP facade, this module is
the query. Same split as :mod:`apps.webui.server.routes.ingest_job`.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from apps.adapters.rekordbox import config as rb_config
from apps.adapters.rekordbox.paths import resolve_asset_path
from apps.analysis.record import AnalysisRecord
from apps.analysis_waveform.local_waveform import local_preview_strip
from apps.shared.platform_paths import AssetResolver
from apps.webui.server.rb_vendor_pkg.anlz import preview_strip
from apps.webui.server.rb_vendor_pkg.own_beatgrid_overlay import _beatgrid_payload
from apps.webui.server.rb_vendor_pkg.track_rows import bulk_rb_meta
from apps.webui.server.routes.analysis import synthesize_fallback_beats
from apps.webui.server.routes.ingest_job import (
    tracks_on_disk,
    valid_stem_bundle_ids,
)

ReadinessAxis = Literal[
    "not_ready",
    "beatgrid",
    "waveform",
    "stems",
    "stems_corrupt",
    "sync",
    "analysis",
    "all",
]
BeatgridStatus = Literal["ok", "missing", "invalid"]
WaveformStatus = Literal["ok", "missing"]
StemsStatus = Literal["ready", "missing", "corrupt"]
SyncReason = Literal["no_beatgrid", "invalid_grid"]

_OWN_BACKEND_LIKE = "own\\_%"
_ANALYSIS_RECORD_INVALID = "ANALYSIS_RECORD_INVALID"


class LibraryReadinessItem(BaseModel):
    """One present track's stored-artifact readiness."""

    stable_id: str
    title: str | None
    file_path: str
    beatgrid: BeatgridStatus
    waveform_preview: WaveformStatus
    stems: StemsStatus
    has_analysis: bool
    analysis_backend: str | None
    analysis_version: str | None
    sync_compatible: bool
    sync_reason: SyncReason | None
    gaps: list[str]


class LibraryReadinessCounts(BaseModel):
    """Every field is a count over ``present``, never over ``total_tracks``."""

    ready: int
    not_ready: int
    beatgrid_ok: int
    beatgrid_missing: int
    beatgrid_invalid: int
    waveform_ok: int
    waveform_missing: int
    stems_ready: int
    stems_missing: int
    stems_corrupt: int
    sync_compatible: int
    sync_incompatible: int
    has_analysis: int
    missing_analysis: int


class LibraryReadinessOut(BaseModel):
    generated_at: float
    denominator: Literal["present"] = "present"
    present: int
    total_tracks: int
    unreachable: int
    counts: LibraryReadinessCounts
    items: list[LibraryReadinessItem]


@dataclass
class _AnalysisIndex:
    has_row: set[str] = field(default_factory=set)
    version: dict[str, tuple[str, str]] = field(default_factory=dict)
    own_grid: dict[str, tuple[BeatgridStatus, list[dict[str, Any]]]] = field(
        default_factory=dict
    )
    legacy: dict[str, AnalysisRecord] = field(default_factory=dict)


def _finite_number(value: object, *, positive: bool) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    if positive and number <= 0:
        return None
    if not positive and number < 0:
        return None
    return number


def _validated_beat(beat: object) -> tuple[int, float] | None:
    """``(n, t)`` when one beat matches ``validateBeatGrid``'s per-entry rules."""
    if not isinstance(beat, dict):
        return None
    n = beat.get("n")
    if isinstance(n, bool) or not isinstance(n, int) or n < 1 or n > 4:
        return None
    if _finite_number(beat.get("bpm"), positive=True) is None:
        return None
    t = _finite_number(beat.get("t"), positive=False)
    if t is None:
        return None
    return n, t


def _grid_is_sync_compatible(beats: Sequence[dict[str, Any]]) -> bool:
    """Python transcription of ``validateBeatGrid`` (beat-sync-math.ts)."""
    if len(beats) < 2:
        return False
    previous_t = -1.0
    previous_n: int | None = None
    for i, beat in enumerate(beats):
        parsed = _validated_beat(beat)
        if parsed is None:
            return False
        n, t = parsed
        if i > 0 and t <= previous_t:
            return False
        if previous_n is not None:
            expected = 1 if previous_n == 4 else previous_n + 1
            if n != expected:
                return False
        previous_t = t
        previous_n = n
    return True


def _shape_beats(raw: Sequence[Any]) -> list[dict[str, Any]] | None:
    try:
        return [
            {"n": int(beat["n"]), "bpm": float(beat["bpm"]), "t": float(beat["t"])}
            for beat in raw
        ]
    except (KeyError, TypeError, ValueError):
        return None


def _own_lane_grid(raw: str | None) -> tuple[BeatgridStatus, list[dict[str, Any]]]:
    if raw is None:
        return "invalid", []
    try:
        record = AnalysisRecord.from_json(raw)
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return "invalid", []
    result = record.lanes.get("beatgrid")
    if result is None or result.status != "ok":
        return "invalid", []
    beats = _shape_beats(result.payload.get("beats") or [])
    if not beats:
        return "invalid", []
    return "ok", beats


def _legacy_grid(record: AnalysisRecord) -> tuple[BeatgridStatus, list[dict[str, Any]]]:
    if not record.downbeats_s:
        return "missing", []
    try:
        synthesized = synthesize_fallback_beats(record)
    except Exception as exc:
        detail = getattr(exc, "detail", None)
        if isinstance(detail, dict) and detail.get("code") == _ANALYSIS_RECORD_INVALID:
            return "invalid", []
        raise
    if not synthesized:
        return "missing", []
    beats = [{"n": b.n, "bpm": b.bpm, "t": b.t} for b in synthesized]
    return "ok", beats


def _cached_pqtz(stable_id: str) -> tuple[BeatgridStatus, list[dict[str, Any]]] | None:
    path = rb_config.ANLZ_CACHE_DIR / f"{stable_id}.json"
    if not path.is_file():
        return None
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    payload = cached.get("payload")
    if not isinstance(payload, dict):
        return None
    block = payload.get("beatgrid")
    if not isinstance(block, dict):
        return None
    beats = _shape_beats(block.get("beats") or [])
    count = int(block.get("beat_count") or 0)
    if count >= 1 and beats:
        return "ok", beats
    return "invalid", []


def _peek_pqtz(
    stable_id: str, analysis_data_path: str
) -> tuple[BeatgridStatus, list[dict[str, Any]]] | None:
    """Rekordbox PQTZ grid, or None when no ANLZ file exists (fall through)."""
    cached = _cached_pqtz(stable_id)
    if cached is not None:
        return cached
    mapped = resolve_asset_path(analysis_data_path)
    if mapped.resolved is None or not mapped.resolved.is_file():
        return None
    try:
        from pyrekordbox.anlz import AnlzFile

        anlz_file = AnlzFile.parse_file(str(mapped.resolved))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return "invalid", []
    tags: dict[str, Any] = {}
    for tag in anlz_file.tags:
        tags.setdefault(tag.type, tag)
    try:
        grid, _times = _beatgrid_payload(tags)
    except (TypeError, ValueError, KeyError, AttributeError):
        return "invalid", []
    beats = _shape_beats(grid.get("beats") or [])
    if int(grid.get("beat_count") or 0) < 1 or not beats:
        return "invalid", []
    return "ok", beats


def _fill_canonical(conn: sqlite3.Connection, index: _AnalysisIndex) -> None:
    pointers: dict[str, list[tuple[str, str, str]]] = {}
    for sid, lane, backend, version in conn.execute(
        "SELECT stable_id, lane, backend, backend_version FROM analysis_canonical"
    ):
        pointers.setdefault(str(sid), []).append(
            (str(lane), str(backend), str(version))
        )
    for sid, _backend, _version, raw in conn.execute(
        "SELECT c.stable_id, c.backend, c.backend_version, a.record_json "
        "FROM analysis_canonical c LEFT JOIN analysis a "
        "ON a.stable_id = c.stable_id AND a.backend = c.backend "
        "AND a.backend_version = c.backend_version "
        "WHERE c.lane = 'beatgrid'"
    ):
        index.own_grid[str(sid)] = _own_lane_grid(raw)
    for sid, rows in pointers.items():
        beat = next((row for row in rows if row[0] == "beatgrid"), None)
        chosen = beat if beat is not None else min(rows, key=lambda row: row[0])
        index.version[sid] = (chosen[1], chosen[2])


def _fill_legacy(conn: sqlite3.Connection, index: _AnalysisIndex) -> None:
    for sid, backend, version, raw in conn.execute(
        "SELECT stable_id, backend, backend_version, record_json FROM analysis "
        "WHERE backend NOT LIKE ? ESCAPE '\\' "
        "ORDER BY analyzed_at DESC, backend ASC, backend_version DESC",
        (_OWN_BACKEND_LIKE,),
    ):
        key = str(sid)
        if key in index.legacy:
            continue
        try:
            index.legacy[key] = AnalysisRecord.from_json(raw)
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            continue
        index.version.setdefault(key, (str(backend), str(version)))


def _load_analysis_index(conn: sqlite3.Connection) -> _AnalysisIndex:
    names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('analysis', 'analysis_canonical')"
        )
    }
    if "analysis" not in names:
        return _AnalysisIndex()
    index = _AnalysisIndex(
        has_row={
            row[0] for row in conn.execute("SELECT DISTINCT stable_id FROM analysis")
        }
    )
    if "analysis_canonical" in names:
        _fill_canonical(conn, index)
    _fill_legacy(conn, index)
    return index


def _beatgrid_for(
    sid: str,
    index: _AnalysisIndex,
    rb_meta: dict[str, Any],
) -> tuple[BeatgridStatus, list[dict[str, Any]]]:
    if sid in index.own_grid:
        return index.own_grid[sid]
    meta = rb_meta.get(sid)
    adp = getattr(meta, "analysis_data_path", None) if meta is not None else None
    if adp:
        peeked = _peek_pqtz(sid, adp)
        if peeked is not None:
            return peeked
    record = index.legacy.get(sid)
    if record is not None:
        return _legacy_grid(record)
    return "missing", []


def _waveform_ok(sid: str, rb_meta: dict[str, Any], resolver: AssetResolver) -> bool:
    meta = rb_meta.get(sid)
    if meta is not None:
        preview_b64, _preview_max = preview_strip(
            meta.analysis_data_path, resolver=resolver
        )
        return preview_b64 is not None
    preview_b64, _preview_max = local_preview_strip(sid)
    return preview_b64 is not None


def _stems_status(sid: str, done: set[str], corrupt: set[str]) -> StemsStatus:
    if sid in done:
        return "ready"
    if sid in corrupt:
        return "corrupt"
    return "missing"


def _sync_verdict(
    beatgrid: BeatgridStatus, beats: list[dict[str, Any]]
) -> tuple[bool, SyncReason | None]:
    if beatgrid == "missing":
        return False, "no_beatgrid"
    if beatgrid == "invalid" or not _grid_is_sync_compatible(beats):
        return False, "invalid_grid"
    return True, None


def _gaps(
    *,
    beatgrid: BeatgridStatus,
    waveform: WaveformStatus,
    stems: StemsStatus,
    sync_ok: bool,
    has_analysis: bool,
) -> list[str]:
    gaps: list[str] = []
    if beatgrid != "ok":
        gaps.append("beatgrid")
    if waveform != "ok":
        gaps.append("waveform")
    if stems != "ready":
        gaps.append("stems")
    if not sync_ok:
        gaps.append("sync")
    if not has_analysis:
        gaps.append("analysis")
    return gaps


def _axis_match(item: LibraryReadinessItem, axis: ReadinessAxis) -> bool:
    if axis == "all":
        return True
    if axis == "not_ready":
        return (
            item.beatgrid != "ok"
            or item.waveform_preview != "ok"
            or item.stems != "ready"
            or not item.sync_compatible
        )
    if axis == "beatgrid":
        return item.beatgrid != "ok"
    if axis == "waveform":
        return item.waveform_preview != "ok"
    if axis == "stems":
        return item.stems != "ready"
    if axis == "stems_corrupt":
        return item.stems == "corrupt"
    if axis == "sync":
        return not item.sync_compatible
    return not item.has_analysis


def _empty_counts() -> LibraryReadinessCounts:
    return LibraryReadinessCounts(
        ready=0,
        not_ready=0,
        beatgrid_ok=0,
        beatgrid_missing=0,
        beatgrid_invalid=0,
        waveform_ok=0,
        waveform_missing=0,
        stems_ready=0,
        stems_missing=0,
        stems_corrupt=0,
        sync_compatible=0,
        sync_incompatible=0,
        has_analysis=0,
        missing_analysis=0,
    )


def _bump(counts: LibraryReadinessCounts, item: LibraryReadinessItem, ready: bool) -> None:
    if ready:
        counts.ready += 1
    else:
        counts.not_ready += 1
    if item.beatgrid == "ok":
        counts.beatgrid_ok += 1
    elif item.beatgrid == "missing":
        counts.beatgrid_missing += 1
    else:
        counts.beatgrid_invalid += 1
    if item.waveform_preview == "ok":
        counts.waveform_ok += 1
    else:
        counts.waveform_missing += 1
    if item.stems == "ready":
        counts.stems_ready += 1
    elif item.stems == "missing":
        counts.stems_missing += 1
    else:
        counts.stems_corrupt += 1
    if item.sync_compatible:
        counts.sync_compatible += 1
    else:
        counts.sync_incompatible += 1
    if item.has_analysis:
        counts.has_analysis += 1
    else:
        counts.missing_analysis += 1


def _titles_and_total(
    conn: sqlite3.Connection, present_ids: Sequence[str]
) -> tuple[dict[str, str | None], int]:
    total = conn.execute(
        "SELECT count(*) FROM tracks WHERE deleted_at IS NULL"
    ).fetchone()[0]
    titles: dict[str, str | None] = {}
    if not present_ids:
        return titles, int(total)
    chunk = 500
    for start in range(0, len(present_ids), chunk):
        batch = list(present_ids[start : start + chunk])
        placeholders = ",".join("?" * len(batch))
        for sid, title in conn.execute(
            f"SELECT stable_id, title FROM tracks WHERE stable_id IN ({placeholders})",
            batch,
        ):
            titles[str(sid)] = title
    return titles, int(total)


def query_library_readiness(
    conn_factory: Callable[[], sqlite3.Connection],
    stem_roots: Sequence[Path],
    *,
    limit: int = 200,
    axis: ReadinessAxis = "not_ready",
) -> LibraryReadinessOut:
    """Full-population counts plus an ``axis``-filtered, ``limit``-capped list."""
    on_disk, unreachable = tracks_on_disk(conn_factory)
    present_ids = [sid for sid, _fp in on_disk]
    paths = {sid: fp for sid, fp in on_disk}
    stems_done, stems_corrupt = valid_stem_bundle_ids(stem_roots)
    conn = conn_factory()
    try:
        titles, total_tracks = _titles_and_total(conn, present_ids)
        index = _load_analysis_index(conn)
    finally:
        conn.close()
    rb_meta = bulk_rb_meta(present_ids) if present_ids else {}
    resolver = AssetResolver()
    counts = _empty_counts()
    matched: list[LibraryReadinessItem] = []
    for sid in present_ids:
        beatgrid, beats = _beatgrid_for(sid, index, rb_meta)
        waveform: WaveformStatus = (
            "ok" if _waveform_ok(sid, rb_meta, resolver) else "missing"
        )
        stems = _stems_status(sid, stems_done, stems_corrupt)
        sync_ok, sync_reason = _sync_verdict(beatgrid, beats)
        has_analysis = sid in index.has_row
        backend_version = index.version.get(sid)
        ready = (
            beatgrid == "ok"
            and waveform == "ok"
            and stems == "ready"
            and sync_ok
        )
        item = LibraryReadinessItem(
            stable_id=sid,
            title=titles.get(sid),
            file_path=paths[sid],
            beatgrid=beatgrid,
            waveform_preview=waveform,
            stems=stems,
            has_analysis=has_analysis,
            analysis_backend=backend_version[0] if backend_version else None,
            analysis_version=backend_version[1] if backend_version else None,
            sync_compatible=sync_ok,
            sync_reason=sync_reason,
            gaps=_gaps(
                beatgrid=beatgrid,
                waveform=waveform,
                stems=stems,
                sync_ok=sync_ok,
                has_analysis=has_analysis,
            ),
        )
        _bump(counts, item, ready)
        if _axis_match(item, axis):
            matched.append(item)
    matched.sort(key=lambda item: item.stable_id)
    return LibraryReadinessOut(
        generated_at=time.time(),
        present=len(on_disk),
        total_tracks=total_tracks,
        unreachable=unreachable,
        counts=counts,
        items=matched[:limit],
    )


__all__ = [
    "LibraryReadinessCounts",
    "LibraryReadinessItem",
    "LibraryReadinessOut",
    "ReadinessAxis",
    "query_library_readiness",
]
