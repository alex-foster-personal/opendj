"""Edge sampler slot sources: the production reads behind the catalogue, behind one seam.

Spec: ``(private path)`` section 3.2 (what a slot carries) and 3.3 (filters).
The catalogue (``sampler_catalogue.py``) asks a :class:`SamplerSources` for a track's grid and
waveform, its listing metadata, its audio file and its stem-set. Production reads the same rows
the deck and the library read (``/anlz``, the track listing, the stem bundle); tests inject an
in-memory implementation on ``app.state.sampler_sources``.

Mini-PRD:
  ✔︎ ✅ 🎯 ``sources_for`` uses the injected sources when the app carries one, else production
    [if] app.state.sampler_sources is set [then] it is the one used [else ⛔️]
  ✔︎ ✅ 🎯 ``library_flags`` are the library filters' own predicates (broken, remix, vocals, offline)
    [if] a row has rekordbox vocal regions or 6 lyric lines [then] vocals is true [else ⛔️]
    [if] a row is streaming or a spotify-pending id [then] offline is false [else ⛔️]
  ✔︎ ✅ 🎯 ``duration_ms`` places the waveform in time from state.db, or refuses
    [if] a track has no duration_ms [then] ValueError, never a guessed duration [else ⛔️]
  ✔︎ ✅ 🎯 stem parts come from the stem-set's own manifest layout, in sampler part names
    [if] a stem-set manifest says demucs4 [then] parts are vocals, drums, bass, inst [else ⛔️]
  ✔︎ ✅ 🎯 ``crates`` are the library's playlists in the playlists pane's own tree order, Inbox left out
    [if] the user ordered one crate first [then] it leads, the rest follow by name [else ⛔️]
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from fastapi import Request

from apps.shared import runtime_policy
from apps.shared.state import db as state_db
from apps.stems.artifacts import STEM_LAYOUTS, StemBundleNotFoundError, load_stem_bundle, stem_roots
from apps.stems.cli import resolve_audio_path

from . import playlist_tree_order as tree_order
from . import rb_vendor
from .backend import Playlist, StateBackend
from .deps import get_library_data_dir
from .inbox_playlist import INBOX_PLAYLIST_ID
from .routes.grid_edit import state_db_path
from .routes.playlists import _playlist_summaries
from .routes.rb_assets import served_anlz_payload
from .routes.stems_bundle import _read_barrier, _stem_roots, _stems_dir
from .routes.tracks import _listing_row_from_builder
from .sampler_core import TrackAnalysis

#: A stem-set's own part names -> the SamplerVector part names (sampler-types.ts): INST is
#: "other" in the Demucs four and "instrumental" in a RoFormer pair, as the deck's capture maps it.
STEM_PART_OF_BUNDLE = {"vocals": "vocals", "drums": "drums", "bass": "bass", "other": "inst", "instrumental": "inst"}
BANDS = ("low", "mid", "high")


@dataclass(frozen=True)
class TrackMeta:
    """The listing fields a slot shows and filters on."""

    title: str
    artist: str
    key: str | None
    energy: float | None
    flags: dict[str, bool]


class SamplerSources(Protocol):
    """Everything the catalogue and the clip renderer read about a track."""

    def analysis(self, request: Request, stable_id: str) -> TrackAnalysis: ...
    def meta(self, request: Request, backend: StateBackend, stable_ids: Sequence[str]) -> dict[str, TrackMeta]: ...
    def audio_path(self, request: Request, stable_id: str) -> Path: ...
    def stem_parts(self, request: Request, stable_id: str) -> tuple[str, ...] | None: ...
    def stem_part_path(self, request: Request, stable_id: str, part: str) -> Path | None: ...
    def crates(self, request: Request, backend: StateBackend) -> list[tuple[str, ...]]: ...


# ---------- library predicates -------------------------------------------------------


def _vocals_verdict_yes(vocals: dict[str, Any] | None, lyrics: dict[str, Any] | None) -> bool:
    """rowHasVocals in frontend/src/lib/components/rb/browser/vocals-filter.ts, ported."""
    status = (vocals or {}).get("status")
    regions = (vocals or {}).get("regions") or []
    regions_found = status in ("rekordbox", "demucs") and len(regions) > 0
    lines = lyrics.get("n_lines") if lyrics else None
    lyric_lines = lyrics is not None and lyrics.get("effective") != "no-lyrics" and lines is not None and lines >= 6
    return regions_found or lyric_lines or (lyrics is not None and lyrics.get("effective") == "vocal")


def library_flags(row: dict[str, Any], stable_id: str) -> dict[str, bool]:
    """The library filters' predicates (pane-contract.svelte.ts rowIsRemix, rowIsLocallyAvailable)."""
    offline = (
        row.get("is_streaming") is not True
        and not stable_id.startswith("spotify-pending:")
        and row.get("file_exists") is True
    )
    return {
        "broken": row.get("file_exists") is False,
        "remix": row.get("is_remix") is True,
        "vocals": _vocals_verdict_yes(row.get("vocals"), row.get("lyrics")),
        "offline": offline,
    }


def stem_parts_of_manifest(manifest: Path) -> tuple[str, ...] | None:
    """The sampler part names of a stem-set manifest's layout, None for a layout the app does not know."""
    layout = json.loads(manifest.read_text(encoding="utf-8")).get("layout") or "demucs4"
    parts = STEM_LAYOUTS.get(layout)
    return None if parts is None else tuple(STEM_PART_OF_BUNDLE[p] for p in parts)


# ---------- production reads ---------------------------------------------------------


def duration_ms(request: Request, stable_id: str) -> float:
    """The track's state.db duration; refuses a track without one (its waveform cannot be placed)."""
    conn = state_db.open_ro(state_db_path(request))
    try:
        row = conn.execute(
            "SELECT duration_ms FROM tracks WHERE stable_id = ? AND deleted_at IS NULL", (stable_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None or not row[0]:
        raise ValueError(f"track {stable_id} has no duration_ms; its waveform cannot be placed in time")
    return float(row[0])


class ProductionSources:
    """Reads the same grid, waveform and listing rows the deck and library read."""

    def analysis(self, request: Request, stable_id: str) -> TrackAnalysis:
        """The grid and 3-band detail waveform ``/anlz`` serves the deck for this track."""
        payload = served_anlz_payload(request, stable_id, runtime_policy.ANLZ_POINTS_MAX)
        detail = payload["waveform"]["detail"]
        return TrackAnalysis(
            beats_ms=[float(b["t"]) * 1000.0 for b in payload["beatgrid"]["beats"]],
            bands={b: list(detail.get(b) or []) for b in BANDS},
            duration_ms=duration_ms(request, stable_id),
        )

    def meta(self, request: Request, backend: StateBackend, stable_ids: Sequence[str]) -> dict[str, TrackMeta]:
        """Listing metadata for the live tracks among ``stable_ids``, built by the library's row builder."""
        tracks = backend.get_tracks_bulk(list(stable_ids), live_only=True)
        ordered = [tracks[sid] for sid in stable_ids if sid in tracks]
        rows = rb_vendor.build_track_rows(
            ordered,
            jobs_store=getattr(request.app.state, "jobs_store", None),
            data_dir=get_library_data_dir(request),
            state_db_path=state_db_path(request),
        )
        out: dict[str, TrackMeta] = {}
        for track, row in zip(ordered, rows, strict=True):
            item = _listing_row_from_builder(track, row)
            out[track.stable_id] = TrackMeta(
                title=item.get("title") or "",
                artist=item.get("artist") or "",
                key=item.get("key"),
                energy=item.get("energy"),
                flags=library_flags(item, track.stable_id),
            )
        return out

    def audio_path(self, request: Request, stable_id: str) -> Path:
        """The on-disk audio file the stems CLI would run on (raises when there is none)."""
        return resolve_audio_path(get_library_data_dir(request), stable_id)

    def stem_parts(self, request: Request, stable_id: str) -> tuple[str, ...] | None:
        """Cheap listing check: the first stem root holding a manifest names the layout (no file validation)."""
        for root in _stem_roots(request) or stem_roots(_stems_dir(request)):
            manifest = root / stable_id / "manifest.json"
            if manifest.is_file():
                return stem_parts_of_manifest(manifest)
        return None

    def stem_part_path(self, request: Request, stable_id: str, part: str) -> Path | None:
        """The validated stem-set file for one sampler part, None when the track has no stem-set."""
        try:
            with _read_barrier(stable_id, request):
                bundle = load_stem_bundle(stable_id, stems_dir=_stems_dir(request), roots=_stem_roots(request))
        except StemBundleNotFoundError:
            return None
        for bundle_part, path in bundle.files.items():
            if STEM_PART_OF_BUNDLE[bundle_part] == part:
                return Path(path)
        return None

    def crates(self, request: Request, backend: StateBackend) -> list[tuple[str, ...]]:
        """Each crate's member stable ids, crates in the playlists pane's order (compareTreeOrder), Inbox out."""
        playlists = [pl for pl in backend.list_playlists() if pl.playlist_id != INBOX_PLAYLIST_ID]
        user_order = tree_order.load_order(tree_order.path_for(get_library_data_dir(request)))
        seq = {s.playlist_id: s.seq for s in _playlist_summaries(playlists, backend, "skip", user_order)}

        def tree_position(pl: Playlist) -> tuple[Any, ...]:
            position = seq[pl.playlist_id]
            return (position is None, position or 0, pl.name.lower(), pl.playlist_id)

        return [tuple(pl.items) for pl in sorted(playlists, key=tree_position)]


def sources_for(request: Request) -> SamplerSources:
    """The app's injected sampler sources, else the production reads."""
    return getattr(request.app.state, "sampler_sources", None) or ProductionSources()
