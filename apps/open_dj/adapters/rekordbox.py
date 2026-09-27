"""Rekordbox <-> open-dj adapter (OPEN-02 / RB side).

Scope at v0.2 per phase 15 D3:

- **Export (RB -> open-dj)**: full Track + BeatGrid + Playlist; read-only
  CuePoint coverage. Ratings, BPM, key, vendor_ids.
- **Import (open-dj -> RB)**: NOT shipped in this plan. The live write path
  goes through Phase 4's cautious writer (``apps.reconcile.apply``) with
  the 6-rail safety pattern. Phase 16 wires adapter-driven imports.

The adapter is designed so its hot path (``build_library``) takes plain
iterables of dataclasses, not a live DB handle. This makes the adapter
testable with in-memory fixtures and keeps the live-DB opening outside the
core logic (where the 6 safety rails live).
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from apps.open_dj import SCHEMA_VERSION
from apps.open_dj.adapters._base import ExportResult
from apps.open_dj.id import compute_track_id_with_tier
from apps.open_dj.provenance import wrap

name = "rekordbox"


# ---------------------------------------------------------- input dataclasses


@dataclass(slots=True)
class RBTrackInput:
    """Minimal track shape the adapter needs. Maps 1:1 onto apps.shared.rekordbox_db.RBTrack.

    Callers can either build these directly (tests + fixtures) or adapt
    live ``RBTrack`` rows via :func:`from_rbtrack`.
    """

    rb_id: str
    title: str
    artists: list[str]
    album: str = ""
    isrc: str | None = None
    duration_ms: int = 0
    file_path: str | None = None
    size_bytes: int | None = None
    mtime: float | None = None
    bpm: float | None = None
    key: str | None = None
    rating: int | None = None
    genre: str | None = None
    fingerprint: str | None = None
    cue_points: list[dict] | None = None
    beatgrid: dict | None = None
    content_hash_hex: str | None = None
    """Optional pre-computed sha256 hex of the audio file bytes. When None
    the adapter falls back to a deterministic synthetic hash derived from
    ``(size_bytes, mtime, file_path)`` and marks the track with
    ``x_content_hash_mode = "inferred"``.
    """
    updated_at: datetime | None = None
    """The rekordbox DjmdContent row's own ``updated_at`` (StatsFull mixin,
    ``onupdate=datetime.now`` -- rekordbox stamps this itself whenever the
    row changes). This is the SOURCE timestamp for the ``bpm``/``key``/
    ``rating`` provenance envelopes: it says when rekordbox last touched
    the value, not when this tool happened to run an export. Required
    whenever ``bpm``/``key``/``rating`` is set -- see ``_build_track``.
    """


@dataclass(slots=True)
class RBPlaylistInput:
    rb_id: str
    name: str
    track_rb_ids: list[str]
    parent_rb_id: str | None = None


# -------------------------------------------------------- live-DB converters


def from_rbtrack(rb) -> RBTrackInput:
    """Convert an :class:`apps.shared.rekordbox_db.RBTrack` to the adapter input."""
    # pyrekordbox exposes many optional columns that ``RBTrack`` omits;
    # the live adapter bridge can be widened here without touching the
    # hot-path logic.
    artists_raw = rb.artist or ""
    artists = [a.strip() for a in artists_raw.split(",") if a.strip()] or [artists_raw]
    fp = rb.file_path
    # ISRC + duration surfaced on RBTrack as of Phase 15.2 widening.
    duration_ms = round(rb.duration_s * 1000) if rb.duration_s else 0
    return RBTrackInput(
        rb_id=str(rb.id),
        title=rb.title or "",
        artists=artists,
        album=rb.album or "",
        isrc=rb.isrc,
        duration_ms=duration_ms,
        file_path=str(fp) if fp else None,
        size_bytes=rb.file_size,
        mtime=None,
        bpm=rb.bpm,
        rating=rb.rating,
        genre=rb.genre,
        updated_at=rb.updated_at,
    )


# ---------------------------------------------------------------- core build


def build_library(
    tracks: Iterable[RBTrackInput],
    playlists: Iterable[RBPlaylistInput] = (),
    *,
    include_cues: bool = True,
) -> ExportResult:
    """Build a v0.2 library dict from the given RB input iterables.

    Returns an :class:`ExportResult` with the unpacked document. Call
    :func:`apps.open_dj.canon.to_canonical_bytes` on ``result.document`` to
    get the final JCS bytes.
    """
    tracks_out: list[dict] = []
    warnings: list[str] = []
    cue_count = 0
    # rb_id -> open-dj track_id, used when emitting playlists.
    rb_to_track_id: dict[str, str] = {}

    for t in tracks:
        try:
            track_dict, cues = _build_track(t, include_cues=include_cues)
        except ValueError as exc:
            warnings.append(f"track {t.rb_id}: {exc}")
            continue
        rb_to_track_id[t.rb_id] = track_dict["track_id"]
        cue_count += cues
        tracks_out.append(track_dict)

    playlists_out: list[dict] = []
    for p in playlists:
        track_ids = [rb_to_track_id[rid] for rid in p.track_rb_ids
                     if rid in rb_to_track_id]
        pl: dict = {
            "playlist_id": f"rb_pl_{p.rb_id}",
            "name": p.name,
            "tracks_ordered": track_ids,
        }
        if p.parent_rb_id:
            pl["parent_id"] = f"rb_pl_{p.parent_rb_id}"
        playlists_out.append(pl)

    document: dict = {
        "schema_version": SCHEMA_VERSION,
        "kind": "library",
        "tracks": tracks_out,
    }
    if playlists_out:
        document["playlists"] = playlists_out

    return ExportResult(
        document=document,
        tracks_count=len(tracks_out),
        playlists_count=len(playlists_out),
        cue_points_count=cue_count,
        warnings=warnings,
    )


# --------------------------------------------------------------- per-track


def _build_track(t: RBTrackInput, *, include_cues: bool) -> tuple[dict, int]:
    """Return the Track dict + cue count for a single input."""
    track_id, tier = compute_track_id_with_tier({
        "isrc": t.isrc,
        "fingerprint": t.fingerprint,
        "duration_ms": t.duration_ms,
        "size_bytes": t.size_bytes,
        "absolute_path": t.file_path,
        "mtime": t.mtime,
    })
    content_hash, is_synthetic = _content_hash(t)

    track: dict = {
        "track_id": track_id,
        "title": t.title,
        "artists": list(t.artists),
        "duration_ms": int(t.duration_ms or 0),
        "file_path": t.file_path or "",
        "content_hash": content_hash,
        "vendor_ids": {"rekordbox": t.rb_id},
    }
    if t.album:
        track["album"] = t.album
    if t.isrc:
        # Normalise ISRC to canonical form (uppercase, no punctuation).
        from apps.shared.state.ids import normalise_isrc
        normalised = normalise_isrc(t.isrc) or t.isrc
        track["isrc"] = normalised
    if t.size_bytes is not None:
        track["size_bytes"] = int(t.size_bytes)
    _apply_provenance_fields(track, t)
    if t.beatgrid:
        track["beatgrid"] = dict(t.beatgrid)
    if include_cues and t.cue_points:
        track["cue_points"] = [dict(c) for c in t.cue_points]
    if tier == "inferred":
        track["x_track_id_tier"] = "inferred"
    if is_synthetic:
        track["x_content_hash_mode"] = "inferred"
    cues = len(track.get("cue_points", []))
    return track, cues


def _apply_provenance_fields(track: dict, t: RBTrackInput) -> None:
    """Wrap ``bpm``/``key``/``rating`` in a ``ProvenanceValue`` stamped
    from the rekordbox row's own ``updated_at`` -- see ``RBTrackInput
    .updated_at``'s docstring. Split out of ``_build_track`` to keep that
    function's branching under the repo's cyclomatic-complexity ratchet.
    """
    if t.bpm is None and not t.key and t.rating is None:
        return
    source_modified_at = t.updated_at
    if source_modified_at is None:
        # RuntimeError, not ValueError: build_library() catches
        # ValueError per-track to skip malformed rows and keep exporting
        # the rest (see the `except ValueError` there). A missing
        # provenance timestamp is a caller/config bug, not a per-track
        # data-quality issue -- letting it collapse to a buried warning
        # would silently ship a wrong-by-construction export (exactly
        # the failure mode this fix exists to close).
        raise RuntimeError(
            f"track {t.rb_id}: bpm/key/rating is set but no updated_at "
            "timestamp is available. Rekordbox's DjmdContent row always "
            "carries one (StatsFull.updated_at) -- populate "
            "RBTrackInput.updated_at from it rather than letting the "
            "provenance envelope silently stamp the wall clock."
        )
    if t.bpm is not None:
        track["bpm"] = wrap(
            float(t.bpm), source="rekordbox", modified_at=source_modified_at
        )
    if t.key:
        track["key"] = wrap(t.key, source="rekordbox", modified_at=source_modified_at)
    if t.rating is not None:
        track["rating"] = wrap(
            int(t.rating), source="rekordbox", modified_at=source_modified_at
        )


def _content_hash(t: RBTrackInput) -> tuple[str, bool]:
    """Return ``(content_hash_str, is_synthetic)`` for a track.

    When the caller supplied a real audio-bytes sha256 via
    :attr:`RBTrackInput.content_hash_hex`, it is used as-is. Otherwise we
    derive a synthetic but deterministic sha256 from
    ``(size_bytes, mtime, file_path)`` so the schema's pattern is still
    satisfied; the caller can detect this mode via the
    ``x_content_hash_mode`` marker we add to the Track.
    """
    if t.content_hash_hex:
        return f"sha256:{t.content_hash_hex.lower()}", False
    blob = f"{t.size_bytes}|{t.mtime}|{t.file_path}".encode("utf-8")
    digest = hashlib.sha256(blob).hexdigest()
    return f"sha256:{digest}", True


# --------------------------------------------------------- live-DB entry point


def export_library(
    *,
    source_path: Path | None = None,
    out_path: Path | None = None,
    include_cues: bool = True,
) -> ExportResult:
    """Open the live Rekordbox DB (via :mod:`apps.shared.rekordbox_db`),
    export to open-dj format, and optionally write canonical JSON to
    ``out_path``.

    This is the live-DB wrapper; for tests use :func:`build_library`
    directly with synthetic :class:`RBTrackInput` fixtures so no DB handle
    is needed.
    """
    from apps.shared import rekordbox_db  # local import keeps core testable.

    db = rekordbox_db.open_db(source_path)
    try:
        track_inputs = [from_rbtrack(t) for t in rekordbox_db.iter_tracks(db)]
        playlist_inputs = [
            RBPlaylistInput(
                rb_id=p.id, name=p.name, parent_rb_id=p.parent_id,
                track_rb_ids=list(p.track_ids),
            )
            for p in rekordbox_db.iter_playlists(db)
        ]
    finally:
        # pyrekordbox DB handles do not expose an explicit close; rely on GC.
        pass

    result = build_library(track_inputs, playlist_inputs, include_cues=include_cues)

    if out_path is not None:
        from apps.open_dj.canon import to_canonical_bytes
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(to_canonical_bytes(result.document))

    return result
