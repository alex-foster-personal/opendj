"""Edge sampler slot catalogue: every loop of the library as a grid-snapped slot row.

Spec: ``(private path)`` sections 3.1 (where slots come from) and 3.2 (what a slot carries).
The catalogue joins the own cue store's loops with rekordbox's (``sampler_rekordbox``) and the
captures in ``sampler.db`` (``apps.shared.state.sampler_store``), snaps each
to the track's EXISTING grid through the sources seam (``sampler_sources``), and returns slot rows
in the wire shape. Ranking, filtering and paging are the route's (``sampler_core.rank``,
``sampler_core.matches_query``). Derived grids, peaks and listing rows are cached per process,
keyed by a fingerprint of everything they derive from, so a warm build reads no analysis.

Mini-PRD:
  ✔︎ ✅ 🎯 one slot per loop hot cue and memory loop, own and rekordbox, snapped to the grid
    [if] a loop cue starts 30 ms after a beat [then] its slot's in_ms is that beat [else ⛔️]
    [if] the own store holds hot cue A [then] rekordbox's hot loop A is not listed [else ⛔️]
    [if] own and rekordbox hold the same memory loop [then] it is listed once [else ⛔️]
  ✔︎ ✅ 🎯 a track with no usable grid yields no slot and counts in skipped_no_grid
    [if] a track's grid has no beats [then] it has no slot and skipped_no_grid counts it [else ⛔️]
  ✔︎ ✅ 🎯 the catalogue is cached per (stable_id, track fingerprint): 2,000 slots list fast
    [if] 2,000 slots are listed warm [then] it takes under 300 ms and no track is analysed twice [else ⛔️]
  ✔︎ ✅ 🎯 a slot's stored name, description and settings join from sampler.db; recency is its edit stamp
    [if] a slot was renamed through PATCH [then] a cold build lists it with the new name [else ⛔️]
    [if] a slot has no stored settings [then] it lists the defaults [else ⛔️]
  ✔︎ ✅ 🎯 captures from sampler.db list beside the loops, with their vector; recency is their stamp
    [if] a capture is stored [then] it lists as source capture with its vector [else ⛔️]
    [if] a capture is deleted [then] it no longer lists [else ⛔️]
  ✔︎ ✅ 🎯 a slot carries its cue's rekordbox ColorTableIndex as color_index (own cue store or rekordbox)
    [if] a loop hot cue has colour 5 [then] its slot's color_index is 5, a capture's is None [else ⛔️]
  ? starter slots (S14) and vendor-only cue tracks are later slices
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from fastapi import Request

from apps.shared.state import cue_store, sampler_store
from apps.shared.state import db as state_db

from . import sampler_rekordbox
from .backend import StateBackend
from .routes.grid_edit import state_db_path
from .sampler_core import (
    LOOP_CUE_COLOR,
    SOURCE_CAPTURE,
    SOURCE_LOOP,
    SOURCE_MEMORY,
    Region,
    TrackAnalysis,
    region_peaks,
    slot_id_of,
    snap_region,
    usable_grid,
)
from .sampler_models import default_settings
from .sampler_rekordbox import RbLoop
from .sampler_sources import SamplerSources, TrackMeta, sources_for

_log = logging.getLogger(__name__)

_CUE_ROWS_SQL = (
    "SELECT f.stable_id, f.value_json FROM track_fields f JOIN tracks t ON t.stable_id = f.stable_id "
    "WHERE f.field_name = ? AND t.deleted_at IS NULL"
)
_FINGERPRINT_SQL = (
    "SELECT t.stable_id, t.updated_at, "
    "  (SELECT group_concat(f.field_name || '@' || f.modified_at, ',') FROM track_fields f "
    "   WHERE f.stable_id = t.stable_id), "
    "  (SELECT a.state || '@' || a.checked_at FROM track_availability a WHERE a.stable_id = t.stable_id) "
    "FROM tracks t WHERE t.deleted_at IS NULL"
)


@dataclass(frozen=True)
class _RawRegion:
    source: str
    cue_letter: str | None
    in_ms: float
    out_ms: float
    beats: int | None
    color: int | None  # the cue's rekordbox ColorTableIndex; None for a capture or an uncoloured cue


@dataclass(frozen=True)
class _DerivedSlot:
    raw: _RawRegion
    region: Region
    peaks: dict[str, list[float]]


@dataclass(frozen=True)
class _DerivedTrack:
    has_grid: bool
    slots: tuple[_DerivedSlot, ...]


_RegionsKey = tuple[str | None, tuple[RbLoop, ...]]
_DerivedKey = tuple[str, str, tuple[_RawRegion, ...]]


class _Cache:
    """Per-process caches keyed by (stable_id, track fingerprint)."""

    derived: ClassVar[dict[_DerivedKey, _DerivedTrack]] = {}
    meta: ClassVar[dict[tuple[str, str], TrackMeta]] = {}
    # cue blob + rekordbox loops -> regions; parsing 250 cue blobs was half of a warm build
    regions: ClassVar[dict[_RegionsKey, tuple[_RawRegion, ...]]] = {}


def reset_caches() -> None:
    """Forget every cached derivation and the rekordbox read, so the next build starts cold."""
    _Cache.derived, _Cache.meta, _Cache.regions = {}, {}, {}
    sampler_rekordbox.reset_cache()


@dataclass(frozen=True)
class Catalogue:
    """Every slot of the library, the tracks skipped for no grid, and rekordbox's status."""

    slots: list[dict[str, Any]]
    skipped_no_grid: int
    recency: dict[str, str]  # slot_id -> last edit stamp from sampler.db
    rekordbox: str  # "ok" or "unavailable: <reason>"


# ---------- regions ------------------------------------------------------------------


def loop_regions(cue_value_json: str | None, rb_loops: Sequence[RbLoop]) -> list[_RawRegion]:
    """The own row's loops, else rekordbox's: cue_store.load()'s rule, where an own row is the whole cue set
    (ingest copied rekordbox's in) and vendor cues are only the fallback for a track with no row, so a loop
    removed in Open DJ never comes back as a slot; memory loops are deduped on the region."""
    if cue_value_json is not None:
        rb_loops = ()
    own_slots: set[str] = set()
    out: list[_RawRegion] = []
    for raw in json.loads(cue_value_json).get("cues", []) if cue_value_json else []:
        cue = cue_store.cue_view(cue_store.normalize_cue(raw))
        if cue["kind"] == "hot_cue":
            own_slots.add(cue["slot"])
        if cue["is_loop"] and cue["kind"] in ("hot_cue", "loop"):
            source = SOURCE_LOOP if cue["kind"] == "hot_cue" else SOURCE_MEMORY
            out.append(_RawRegion(source, cue["slot"], cue["in_ms"], cue["out_ms"], None, cue["color_table_index"]))
    memory = {(r.in_ms, r.out_ms) for r in out if r.source == SOURCE_MEMORY}
    for loop in rb_loops:
        if loop.slot is not None and loop.slot not in own_slots:
            out.append(_RawRegion(SOURCE_LOOP, loop.slot, loop.in_ms, loop.out_ms, None, loop.color))
        elif loop.slot is None and (loop.in_ms, loop.out_ms) not in memory:
            out.append(_RawRegion(SOURCE_MEMORY, None, loop.in_ms, loop.out_ms, None, loop.color))
    return out


def _fingerprints(conn: sqlite3.Connection, sids: set[str]) -> dict[str, str]:
    """Everything a slot's grid, peaks and library row derive from: track row, fields, availability."""
    return {
        sid: hashlib.sha1(
            f"{upd}|{','.join(sorted((fields or '').split(',')))}|{avail}".encode(), usedforsecurity=False
        ).hexdigest()
        for sid, upd, fields, avail in conn.execute(_FINGERPRINT_SQL).fetchall()
        if sid in sids
    }


def _derive(sources: SamplerSources, request: Request, sid: str, regions: tuple[_RawRegion, ...]) -> _DerivedTrack:
    analysis: TrackAnalysis = sources.analysis(request, sid)
    if not usable_grid(analysis.beats_ms):
        return _DerivedTrack(has_grid=False, slots=())
    slots = []
    for raw in regions:
        region = snap_region(analysis.beats_ms, raw.in_ms, raw.out_ms, raw.beats)
        if region is None:
            _log.warning(
                "sampler: %s %s %s-%s is under a beat or off the grid; no slot", sid, raw.source, raw.in_ms, raw.out_ms
            )
            continue
        slots.append(_DerivedSlot(raw, region, region_peaks(analysis, region.in_ms, region.out_ms)))
    return _DerivedTrack(has_grid=True, slots=tuple(slots))


def _slot_row(
    slot_id: str,
    sid: str,
    d: _DerivedSlot,
    meta: TrackMeta,
    stem_parts: tuple[str, ...] | None,
    stored: dict[str, Any],
    capture: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "slot_id": slot_id,
        "stable_id": sid,
        "source": d.raw.source,
        "cue_letter": d.raw.cue_letter,
        "in_ms": d.region.in_ms,
        "out_ms": d.region.out_ms,
        "beats": d.region.beats,
        "native_bpm": d.region.native_bpm,
        "key": meta.key,
        "energy": meta.energy,
        "color": LOOP_CUE_COLOR,
        "color_index": d.raw.color,
        "title": meta.title,
        "artist": meta.artist,
        "name": stored.get("name"),
        "description": stored.get("description", ""),
        "vector": capture["vector"] if capture else None,
        "settings": stored.get("settings") or default_settings(is_loop=True),
        "flags": meta.flags,
        "peaks": d.peaks,
        "stems_ready": stem_parts is not None,
        "stem_parts": list(stem_parts) if stem_parts is not None else None,
    }


# ---------- build --------------------------------------------------------------------


def build_catalogue(request: Request, backend: StateBackend) -> Catalogue:
    """Every grid-snapped slot of the live library, with the tracks skipped for no grid."""
    stored_by_sid = sampler_store.load_all(state_db_path(request))
    conn = state_db.open_ro(state_db_path(request))
    try:
        cue_rows: dict[str, str] = dict(conn.execute(_CUE_ROWS_SQL, (cue_store.CUE_FIELD,)).fetchall())
        rb = sampler_rekordbox.loop_cues()
        rb_by_sid = sampler_rekordbox.loops_by_stable_id(conn, rb)
        fingerprints = _fingerprints(conn, set(cue_rows) | set(rb_by_sid) | set(stored_by_sid))  # live tracks only
    finally:
        conn.close()

    regions_by_sid: dict[str, tuple[_RawRegion, ...]] = {}
    live_regions: dict[_RegionsKey, tuple[_RawRegion, ...]] = {}
    for sid in sorted(fingerprints):
        rkey = (cue_rows.get(sid), tuple(rb_by_sid.get(sid, ())))
        regions = _Cache.regions.get(rkey)
        if regions is None:
            regions = tuple(loop_regions(*rkey))
        live_regions[rkey] = regions
        captures = (stored_by_sid.get(sid) or sampler_store.TrackSamplerData()).captures
        regions += tuple(_RawRegion(SOURCE_CAPTURE, None, c["in_ms"], c["out_ms"], c["beats"], None) for c in captures)
        if regions:
            regions_by_sid[sid] = regions

    sources = sources_for(request)
    missing_meta = [sid for sid in regions_by_sid if (sid, fingerprints[sid]) not in _Cache.meta]
    if missing_meta:
        for sid, fetched in sources.meta(request, backend, missing_meta).items():
            _Cache.meta[(sid, fingerprints[sid])] = fetched

    slots: list[dict[str, Any]] = []
    recency: dict[str, str] = {}
    skipped = 0
    live: dict[_DerivedKey, _DerivedTrack] = {}
    for sid, regions in regions_by_sid.items():
        key = (sid, fingerprints[sid], regions)
        derived = _Cache.derived.get(key) or _derive(sources, request, sid, regions)
        live[key] = derived
        if not derived.has_grid:
            skipped += 1
            continue
        meta = _Cache.meta.get((sid, fingerprints[sid]))
        if meta is None:
            raise LookupError(f"sampler: live track {sid} has loops but the library listing has no row for it")
        stem_parts = sources.stem_parts(request, sid)  # never cached: a stem-set can land at any time
        stored = stored_by_sid.get(sid) or sampler_store.TrackSamplerData()
        captures_by_region = {(c["in_ms"], c["out_ms"]): c for c in stored.captures}
        for d in derived.slots:
            slot_id = slot_id_of(sid, d.raw.source, d.region)
            row_meta = stored.meta.get(slot_id, {})
            capture = (
                captures_by_region.get((d.region.in_ms, d.region.out_ms)) if d.raw.source == SOURCE_CAPTURE else None
            )
            stamp = row_meta.get("updated_at") or (capture or {}).get("created_at")
            if stamp:
                recency[slot_id] = stamp
            slots.append(_slot_row(slot_id, sid, d, meta, stem_parts, row_meta, capture))
    # Keep only what this library still derives from, so moved cues and old fingerprints never pile up.
    _Cache.derived, _Cache.regions = live, live_regions
    _Cache.meta = {k: v for k, v in _Cache.meta.items() if fingerprints.get(k[0]) == k[1]}
    return Catalogue(slots=slots, skipped_no_grid=skipped, recency=recency, rekordbox=rb.status)
