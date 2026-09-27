"""djay Pro <-> open-dj adapter (OPEN-02 / djay side).

Scope at v0.3 per phase 15 D3:

- **Export (djay -> open-dj)**: Track (with ISRC when available) +
  vendor_ids + Playlist. Cue export is opportunistic -- the djay reader
  surface is still evolving in Phase 4. When cues are present on the input,
  they are emitted tagged with ``source = "djay"``.
- **Import (open-dj -> djay)**: deferred. Reuses
  ``apps/sync/apply_ratings.py`` for the cautious live-rating write (phase
  2 D3 pattern) when Phase 16 wires it.

The adapter takes plain dataclass inputs (:class:`DjayTrackInput`) so the
hot path is testable in-memory. Live-DB export opens the TSAF reader via
:mod:`apps.shared.djay_db`.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from apps.open_dj import SCHEMA_VERSION
from apps.open_dj.adapters._base import ExportResult
from apps.open_dj.id import compute_track_id_with_tier
from apps.open_dj.provenance import wrap

name = "djay"


@dataclass(slots=True)
class DjayTrackInput:
    """Minimal track shape for the djay adapter.

    Maps 1:1 onto :class:`apps.shared.djay_db.DjayTrack` with a few
    optional extension fields the reader does not yet expose but the
    adapter emits when present.
    """

    uuid: str
    title: str
    artist: str
    isrc: str | None
    file_path: str | None
    is_local: bool
    rating: int | None
    duration_s: float | None
    size_bytes: int | None = None
    mtime: float | None = None
    bpm: float | None = None
    key: str | None = None
    fingerprint: str | None = None
    cue_points: list[dict] | None = None
    content_hash_hex: str | None = None


@dataclass(slots=True)
class DjayPlaylistInput:
    uuid: str
    name: str
    track_uuids: list[str]
    parent_uuid: str | None = None


def from_djaytrack(dj) -> DjayTrackInput:
    """Convert :class:`apps.shared.djay_db.DjayTrack` to adapter input."""
    return DjayTrackInput(
        uuid=dj.uuid,
        title=dj.title or "",
        artist=dj.artist or "",
        isrc=dj.isrc,
        file_path=dj.file_path,
        is_local=dj.is_local,
        rating=dj.rating,
        duration_s=dj.duration_s,
    )


def build_library(
    tracks: Iterable[DjayTrackInput],
    playlists: Iterable[DjayPlaylistInput] = (),
    *,
    include_cues: bool = True,
    modified_at: datetime | None = None,
) -> ExportResult:
    """Build a v0.2 library document from djay-side inputs.

    ``modified_at`` stamps the ``bpm``/``key``/``rating`` provenance
    envelopes. djay's ``MediaLibrary.db`` (read via
    :mod:`apps.shared.djay_db`) exposes no per-track last-modified column
    today, unlike rekordbox's DjmdContent.updated_at, so there is no
    source timestamp to prefer here. Rather than have ``_build_track``
    reach for the wall clock per call (which is what silently broke
    determinism in the sibling rekordbox adapter -- OPEN-02), this is an
    explicit, required-when-needed parameter: the caller decides what
    "modified" means for this export and that choice is visible at the
    call site. :func:`export_library` (the live-DB entry point) passes
    ``datetime.now(UTC)`` once, here, rather than letting it leak into
    the per-field wrap() calls.
    """
    tracks_out: list[dict] = []
    warnings: list[str] = []
    cue_count = 0
    uuid_to_track_id: dict[str, str] = {}

    for t in tracks:
        try:
            track_dict, cues = _build_track(
                t, include_cues=include_cues, modified_at=modified_at
            )
        except ValueError as exc:
            warnings.append(f"track {t.uuid}: {exc}")
            continue
        uuid_to_track_id[t.uuid] = track_dict["track_id"]
        cue_count += cues
        tracks_out.append(track_dict)

    playlists_out: list[dict] = []
    for p in playlists:
        track_ids = [uuid_to_track_id[u] for u in p.track_uuids
                     if u in uuid_to_track_id]
        pl: dict = {
            "playlist_id": f"djay_pl_{p.uuid}",
            "name": p.name,
            "tracks_ordered": track_ids,
        }
        if p.parent_uuid:
            pl["parent_id"] = f"djay_pl_{p.parent_uuid}"
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


def _build_track(
    t: DjayTrackInput, *, include_cues: bool, modified_at: datetime | None
) -> tuple[dict, int]:
    duration_ms = int((t.duration_s or 0) * 1000)
    track_id, tier = compute_track_id_with_tier({
        "isrc": t.isrc,
        "fingerprint": t.fingerprint,
        "duration_ms": duration_ms,
        "size_bytes": t.size_bytes,
        "absolute_path": t.file_path,
        "mtime": t.mtime,
    })
    content_hash, is_synthetic = _content_hash(t, duration_ms)

    # Split comma-joined artists similar to RB adapter. djay stores a single
    # string; collaborators are typically ", " separated.
    artists = [a.strip() for a in (t.artist or "").split(",") if a.strip()]
    if not artists:
        artists = [t.artist or ""]

    track: dict = {
        "track_id": track_id,
        "title": t.title,
        "artists": artists,
        "duration_ms": duration_ms,
        "file_path": t.file_path or "",
        "content_hash": content_hash,
        "vendor_ids": {"djay": t.uuid},
    }
    if t.isrc:
        from apps.shared.state.ids import normalise_isrc
        normalised = normalise_isrc(t.isrc) or t.isrc
        track["isrc"] = normalised
    if t.size_bytes is not None:
        track["size_bytes"] = int(t.size_bytes)
    _apply_provenance_fields(track, t, modified_at=modified_at)
    if include_cues and t.cue_points:
        track["cue_points"] = [dict(c) for c in t.cue_points]
    if tier == "inferred":
        track["x_track_id_tier"] = "inferred"
    if is_synthetic:
        track["x_content_hash_mode"] = "inferred"
    if not t.is_local:
        track["x_djay_streaming"] = True
    cues = len(track.get("cue_points", []))
    return track, cues


def _apply_provenance_fields(
    track: dict, t: DjayTrackInput, *, modified_at: datetime | None
) -> None:
    """Wrap ``bpm``/``key``/``rating`` in a ``ProvenanceValue`` stamped
    from the explicit ``modified_at`` clock -- see ``build_library``'s
    docstring for why djay has no per-track source timestamp to prefer.
    Split out of ``_build_track`` to keep that function's branching
    under the repo's cyclomatic-complexity ratchet.
    """
    has_rating = t.rating is not None and t.rating > 0
    if t.bpm is None and not t.key and not has_rating:
        return
    if modified_at is None:
        # RuntimeError, not ValueError: build_library() catches
        # ValueError per-track to skip malformed rows and keep exporting
        # the rest. A missing provenance timestamp is a caller/config
        # bug, not a per-track data-quality issue -- letting it collapse
        # to a buried warning would silently ship a wrong-by-construction
        # export.
        raise RuntimeError(
            f"track {t.uuid}: bpm/key/rating is set but no modified_at "
            "was passed to build_library(). djay exposes no per-track "
            "source timestamp, so this is an explicit clock the caller "
            "must supply -- it must not be guessed as the wall clock "
            "inside this function."
        )
    if t.bpm is not None:
        track["bpm"] = wrap(float(t.bpm), source="djay", modified_at=modified_at)
    if t.key:
        track["key"] = wrap(t.key, source="djay", modified_at=modified_at)
    if has_rating:
        track["rating"] = wrap(int(t.rating), source="djay", modified_at=modified_at)


def _content_hash(t: DjayTrackInput, duration_ms: int) -> tuple[str, bool]:
    """See :func:`apps.open_dj.adapters.rekordbox._content_hash`; mirror here."""
    if t.content_hash_hex:
        return f"sha256:{t.content_hash_hex.lower()}", False
    blob = f"{t.size_bytes}|{t.mtime}|{duration_ms}|{t.file_path}".encode("utf-8")
    return f"sha256:{hashlib.sha256(blob).hexdigest()}", True


def export_library(
    *,
    source_path: Path,
    out_path: Path | None = None,
    include_cues: bool = True,
) -> ExportResult:
    """Open a djay ``MediaLibrary.db`` via :mod:`apps.shared.djay_db` and
    export to open-dj format."""
    from apps.shared import djay_db

    # Delegate connection management to apps.shared.djay_db. The
    # iter_tracks / iter_playlists helpers each open a read-only
    # connection via ``_connect_ro`` (``mode=ro&immutable=1``) so the
    # adapter preserves those PRAGMAs. Do not call ``sqlite3.connect``
    # directly here or the RO guarantee is silently dropped.
    tracks = [from_djaytrack(t) for t in djay_db.iter_tracks(source_path)]
    playlists = [
        DjayPlaylistInput(
            uuid=p.uuid, name=p.name, parent_uuid=p.parent_uuid,
            track_uuids=list(p.track_uuids),
        )
        for p in djay_db.iter_playlists(source_path)
    ]

    # See build_library's docstring: djay has no per-track source
    # timestamp, so "now, once, at the live-export boundary" is the
    # explicit snapshot-time stamp for this run.
    result = build_library(
        tracks, playlists, include_cues=include_cues, modified_at=datetime.now(UTC)
    )

    if out_path is not None:
        from apps.open_dj.canon import to_canonical_bytes
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(to_canonical_bytes(result.document))

    return result
