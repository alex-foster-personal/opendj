"""Serialize the typed :class:`OpenDjLibrary` to the open-dj v0.2 wire format.

The typed dataclass layer in :mod:`apps.open_dj.schema` is a pragmatic
in-memory shape for adapter ``read()``/``write()`` callers. The
**on-disk** shape is the v0.2 JSON Schema at
``open-dj/schema/v0.2/open-dj.schema.json``; the two diverge on:

* provenance envelopes: ``bpm`` / ``key`` / ``rating`` ship as
  ``ProvenanceValue<T>`` on the wire, scalars in memory.
* ``cues``      (tuple of :class:`CuePoint`) -> ``cue_points`` (list).
  The ``loop`` convenience alias splits into a ``loop_in`` event plus a
  derived ``loop_out`` when ``length_ms`` is set.
* ``beats``     (tuple of :class:`BeatGridPoint`) -> ``beatgrid`` object
  with ``origin_ms`` + ``bpm`` taken from the first anchor and all
  positions listed in ``beats``; ``algorithm`` is inferred from whether
  the anchor BPMs vary.
* ``color_rgb`` (int) on cue points -> ``color`` (``#RRGGBB`` hex).
* ``playlists`` nested via ``children`` in memory get flattened using
  synthesized ``playlist_id`` + ``parent_id`` fields.
* top-level ``schema_version`` + ``kind`` get added.
* extension keys not matching ``^x_[a-z0-9][a-z0-9_]*$`` fall under the
  ``patternProperties`` rule and are skipped (with a ``x_`` passthrough
  for typed-only fields such as ``play_count`` and ``color_rgb``).

This is the module the ``open-dj-tool export`` CLI routes class-family
adapter output through (serato / traktor); module-family adapters
(rekordbox / djay) already emit wire-shape documents directly.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from apps.open_dj.provenance import wrap
from apps.open_dj.schema import (
    BeatGridPoint,
    CuePoint,
    OpenDjLibrary,
    Playlist,
    Track,
)

__all__ = ["library_to_wire_document"]


SCHEMA_VERSION = "0.2"
_DEFAULT_SOURCE = "open-dj-tool"


def library_to_wire_document(
    library: OpenDjLibrary,
    *,
    source: str = _DEFAULT_SOURCE,
    modified_at: datetime | str,
) -> dict[str, Any]:
    """Return the v0.2 wire-format ``dict`` for ``library``.

    ``source`` is the :mod:`apps.open_dj.provenance` enum value stamped
    onto authored fields (``bpm``, ``key``, ``rating``). ``modified_at``
    stamps every provenance envelope in the document and is REQUIRED: the
    typed :class:`apps.open_dj.schema.Track` layer has no per-track source
    timestamp of its own (class-family serato/traktor adapters do not
    expose one yet), so this function does not guess one via the wall
    clock -- the caller (``open-dj-tool export``) decides and passes an
    explicit value, once, at the export boundary. Passing ``datetime.now()``
    there is a legitimate choice for a snapshot-time stamp; defaulting to
    it silently inside this serializer is not, because it makes two calls
    for the same untouched library disagree on output bytes whenever they
    straddle a wall-clock second.
    """
    doc: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "kind": "library",
        "tracks": [
            _track_to_wire(t, source=source, modified_at=modified_at)
            for t in library.tracks
        ],
    }

    playlists = _flatten_playlists(library.playlists)
    if playlists:
        doc["playlists"] = playlists

    # Forward x_*-prefixed top-level extensions verbatim; strip the rest
    # (additionalProperties: false on the root is lenient via the
    # ``^x_[a-z0-9][a-z0-9_]*$`` pattern, but not for arbitrary names).
    for k, v in (library.extensions or {}).items():
        if _is_extension_key(k):
            doc[k] = v

    return doc


def _apply_optional_scalars(
    track: dict[str, Any],
    t: Track,
    *,
    source: str,
    modified_at: datetime | str,
) -> None:
    if t.album:
        track["album"] = t.album
    if t.isrc:
        track["isrc"] = t.isrc
    if t.bpm is not None:
        track["bpm"] = wrap(float(t.bpm), source=source, modified_at=modified_at)
    if t.key_camelot:
        track["key"] = wrap(t.key_camelot, source=source, modified_at=modified_at)
    if t.rating is not None:
        track["rating"] = wrap(int(t.rating), source=source, modified_at=modified_at)


def _track_to_wire(
    t: Track, *, source: str, modified_at: datetime | str
) -> dict[str, Any]:
    track: dict[str, Any] = {
        "track_id": t.track_id,
        "title": t.title,
        "artists": list(t.artists),
        "duration_ms": int(t.duration_ms or 0),
        "file_path": t.file_path,
    }

    # content_hash is required by the schema. The typed layer doesn't
    # carry one, so we look in ``extensions`` for a pre-computed value
    # (adapters such as Serato stash one there) and fall back to a
    # passthrough placeholder that the schema regex rejects -- callers
    # that need a valid doc must supply ``extensions["content_hash"]``.
    ext = dict(t.extensions or {})
    content_hash = ext.pop("content_hash", None)
    if content_hash is not None:
        track["content_hash"] = str(content_hash)

    _apply_optional_scalars(track, t, source=source, modified_at=modified_at)

    cue_points = _cues_to_wire(t.cues, source=source, modified_at=modified_at)
    if cue_points:
        track["cue_points"] = cue_points

    beatgrid = _beats_to_wire(
        t.beats, fallback_bpm=t.bpm, source=source, modified_at=modified_at
    )
    if beatgrid is not None:
        track["beatgrid"] = beatgrid

    # Typed-only scalars that the wire schema doesn't model -> x_ passthrough.
    if t.play_count:
        track["x_play_count"] = int(t.play_count)
    if t.color_rgb is not None:
        track["x_color_rgb"] = int(t.color_rgb)

    for k, v in ext.items():
        if _is_extension_key(k):
            track[k] = v

    return track


def _cues_to_wire(
    cues: tuple[CuePoint, ...],
    *,
    source: str,
    modified_at: datetime | str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in cues:
        base: dict[str, Any] = {
            "position_ms": int(c.position_ms),
        }
        if c.name:
            base["name"] = c.name
        if c.color_rgb is not None:
            base["color"] = _color_int_to_hex(c.color_rgb)
        base["source"] = wrap(source, source=source, modified_at=modified_at)

        if c.type == "loop":
            # Split the convenience alias into loop_in / loop_out pair.
            loop_in = dict(base)
            loop_in["type"] = "loop_in"
            if c.length_ms is not None:
                loop_in["length_ms"] = int(c.length_ms)
            out.append(loop_in)
            if c.length_ms is not None:
                loop_out = {
                    "position_ms": int(c.position_ms) + int(c.length_ms),
                    "type": "loop_out",
                    "source": wrap(
                        source, source=source, modified_at=modified_at
                    ),
                }
                if c.name:
                    loop_out["name"] = c.name
                if c.color_rgb is not None:
                    loop_out["color"] = _color_int_to_hex(c.color_rgb)
                out.append(loop_out)
        else:
            evt = dict(base)
            evt["type"] = c.type
            if c.length_ms is not None:
                evt["length_ms"] = int(c.length_ms)
            out.append(evt)
    return out


def _beats_to_wire(
    beats: tuple[BeatGridPoint, ...],
    *,
    fallback_bpm: float | None,
    source: str,
    modified_at: datetime | str,
) -> dict[str, Any] | None:
    if not beats:
        return None
    first = beats[0]
    bpms = {round(b.bpm, 6) for b in beats}
    algorithm = "constant" if len(bpms) <= 1 else "variable"
    grid: dict[str, Any] = {
        "origin_ms": float(first.position_ms),
        "bpm": float(first.bpm or (fallback_bpm or 0.0)),
        "algorithm": algorithm,
        "beats": [float(b.position_ms) for b in beats],
        "source": wrap(source, source=source, modified_at=modified_at),
    }
    return grid


def _flatten_playlists(
    playlists: tuple[Playlist, ...],
) -> list[dict[str, Any]]:
    """Flatten nested playlists into the wire's flat list + parent_id shape.

    ``playlist_id`` is synthesised from the parent chain + name so nested
    folders with duplicate names stay distinct. The open-dj wire doesn't
    model folders natively; emitting a flat list with ``parent_id`` is
    the v0.2 way.
    """
    out: list[dict[str, Any]] = []

    def _walk(pl: Playlist, parent_id: str | None, path: str) -> None:
        pl_id = _slug_join(path, pl.name)
        entry: dict[str, Any] = {
            "playlist_id": pl_id,
            "name": pl.name,
            "tracks_ordered": list(pl.track_ids),
        }
        if parent_id is not None:
            entry["parent_id"] = parent_id
        out.append(entry)
        for child in pl.children:
            _walk(child, pl_id, pl_id)

    for pl in playlists:
        _walk(pl, None, "")
    return out


def _slug_join(path: str, name: str) -> str:
    slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in name) or "pl"
    return f"{path}/{slug}" if path else slug


def _color_int_to_hex(color_rgb: int) -> str:
    return f"#{int(color_rgb) & 0xFFFFFF:06x}"


def _is_extension_key(key: str) -> bool:
    if not key.startswith("x_") or len(key) < 3:
        return False
    rest = key[2:]
    return rest[0].isalnum() and rest[0] == rest[0].lower() and all(
        c.isalnum() and c == c.lower() or c == "_" for c in rest
    )
