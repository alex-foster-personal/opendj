"""Row projection, pairing and fallback-warning helpers for :mod:`.sqlite_backend`."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime
from typing import Any

from apps.analysis.selection import EffectiveField
from apps.analysis_key.canon import canonical_key_text
from apps.shared.pairings.repo import PairingsRepo
from apps.shared.state.writer import StateWriter

from .analysis_overlay import lane_owned_fields as _lane_owned_fields
from .analysis_overlay import selection_tag as _selection_tag
from .backend import (
    AppliedState,
    BackendError,
    ConflictError,
    NotFoundError,
    Pairing,
    Provenance,
    Track,
    TrackFilter,
    applied_state_of,
    resolve_auto_gain_user_db_write,
    resolve_grid_flag_dismissed_write,
    resolve_tempo_pref_write,
    resolve_tracklist_hidden_write,
)
from .etag import compute_etag, strip_quotes

# Same logger as sqlite_backend so its fallback warnings keep their name.
log = logging.getLogger(__name__.removesuffix("_rows"))


# Track which fallback warnings we've already emitted, keyed by method name.
# Per-process singleton so the logs stay quiet after the first call.
_WARNED_LOCK = threading.Lock()
_WARNED: set[str] = set()


def _warn_fallback_once(method: str, reason: str) -> None:
    """Emit a WARNING the first time ``method`` falls back; no-op thereafter."""
    with _WARNED_LOCK:
        if method in _WARNED:
            return
        _WARNED.add(method)
    log.warning(
        "SqliteBackend.%s: falling back to InMemoryBackend (%s)",
        method,
        reason,
    )


def _reset_warnings_for_tests() -> None:
    """Test hook: clear the once-per-process warning cache."""
    with _WARNED_LOCK:
        _WARNED.clear()


# --- field projection ----------------------------------------------------

# Fields the webui Track exposes that live in track_fields (EAV). Any field
# name listed here is JSON-decoded on the way out.
_EAV_FIELDS: tuple[str, ...] = (
    "bpm",
    "key",
    "rating",
    "color_id",
    "tags",
    "notes",
    "last_played_at",
    "genre",
    "comments",
    "energy",
    "tempo_pref",
    "grid_flag_dismissed",
    "tracklist_hidden",
    "auto_gain_user_db",
    # Issue #10029: the file's own year and record label (Year and Label columns).
    "year",
    "label",
)

_TRACKS_PROJECTION = (
    "SELECT stable_id, title, artists_json, album, "
    "       duration_ms, file_path, created_at, updated_at "
    "FROM tracks WHERE deleted_at IS NULL"
)

_TRACKS_PROJECTION_INCLUDE_DELETED = (
    "SELECT stable_id, title, artists_json, album,        duration_ms, file_path, created_at, updated_at FROM tracks"
)


def _parse_rfc3339(ts: str) -> datetime:
    """Parse an RFC 3339 timestamp (``Z`` or ``+00:00`` form). Fail fast.

    Naive timestamps are treated as UTC (legacy fixture rows). Anything
    unparseable raises ``ValueError`` -- no silent clock guessing.
    """
    raw = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _effective_updated_at(
    row_updated_at: str,
    fields: dict[str, EffectiveField],
) -> str:
    """Latest of the row ``updated_at`` and every field ``modified_at``.

    This is the etag input (see module docstring): field-only writes via
    ``StateWriter.set_field`` must advance the etag even though they never
    touch the ``tracks`` identity row. Returns the original string of the
    winning stamp so the derivation is byte-stable across restarts.

    A field with an EMPTY stamp is skipped: an own lane with no analysis
    record yet reports ``status: missing`` and no time, and there is no
    stamp to compare. Parsing "" here would raise on every unanalyzed
    track the moment a lane is switched to own.
    """
    best = row_updated_at
    best_dt = _parse_rfc3339(row_updated_at)
    for view in fields.values():
        if not view.modified_at:
            continue
        dt = _parse_rfc3339(view.modified_at)
        if dt > best_dt:
            best, best_dt = view.modified_at, dt
    return best


def _utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _pairing_id(from_id: str, to_id: str, direction: str) -> str:
    """Stable id for one stored edge, derived from its primary key.

    The table predates the wire's ``pairing_id`` and the CLI writes rows
    without one, so the id is a pure function of the key: the same edge has
    the same id in every process and after every restart.
    """
    digest = hashlib.sha1(f"{from_id}\x1f{to_id}\x1f{direction}".encode()).hexdigest()
    return f"pair-{digest[:24]}"


def _pairing_matches(
    pairing: Pairing,
    *,
    from_stable_id: str | None,
    to_stable_id: str | None,
    source: str | None,
) -> bool:
    """Apply the list route's optional filters to one wire pairing."""
    return (
        (not from_stable_id or pairing.from_stable_id == from_stable_id)
        and (not to_stable_id or pairing.to_stable_id == to_stable_id)
        and (not source or pairing.source == source)
    )


def _row_to_pairing(row: tuple[Any, ...]) -> Pairing:
    """Map a stored pairings row onto the wire's :class:`Pairing`.

    An ``out_of`` edge (CLI only) reads as its reverse ``->`` so every
    pairing the UI shows points the way the mix runs.
    """
    from_id, to_id, direction, source, notes = row[0], row[1], row[2], row[3], row[4]
    snapshot = None if row[7] is None else json.loads(row[7])
    pairing_id = _pairing_id(from_id, to_id, direction)
    if direction == "out_of":
        from_id, to_id = to_id, from_id
    return Pairing(
        pairing_id=pairing_id,
        from_stable_id=from_id,
        to_stable_id=to_id,
        direction="<->" if direction == "either" else "->",
        source=source,
        notes=notes,
        snapshot=snapshot,
        created_at=row[5],
        updated_at=row[6],
    )


def _graph_pairings(conn: sqlite3.Connection) -> list[Pairing]:
    """Wire view of the durable graph ``pairings`` table.

    A missing table reads as empty. A table from before ``snapshot_json``
    still lists; that column is projected as NULL until a write upgrades it.
    """
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pairings'").fetchone()
    if exists is None:
        return []
    columns = {col[1] for col in conn.execute("PRAGMA table_info(pairings)")}
    snapshot_col = "snapshot_json" if "snapshot_json" in columns else "NULL"
    rows = conn.execute(
        "SELECT from_stable_id, to_stable_id, direction, source, "
        f"notes, created_at, modified_at, {snapshot_col} "
        "FROM pairings"
    ).fetchall()
    return [_row_to_pairing(tuple(row)) for row in rows]


def _delete_graph_pairing(
    conn: sqlite3.Connection,
    pairing_id: str,
    *,
    expected_etag: str,
    writer: StateWriter,
) -> None:
    """CAS-delete one graph edge addressed by its derived pairing id.

    HTTP ids are handled by :func:`delete_http_pairing`. This path is the
    graph table's own id (a CLI edge the HTTP table does not store).
    """
    rows = conn.execute(
        "SELECT from_stable_id, to_stable_id, direction, source, "
        "notes, created_at, modified_at, snapshot_json FROM pairings"
    ).fetchall()
    match = None
    for row in rows:
        if _pairing_id(row[0], row[1], row[2]) == pairing_id:
            match = row
            break
    if match is None:
        raise NotFoundError(f"pairing not found: {pairing_id}")
    existing = _row_to_pairing(tuple(match))
    current = compute_etag(existing.pairing_id, existing.updated_at)
    if strip_quotes(current) != strip_quotes(expected_etag):
        raise ConflictError(
            current={"pairing_id": existing.pairing_id, "updated_at": existing.updated_at},
            etag=current,
        )
    PairingsRepo(conn, ensure_schema=False, writer=writer).remove(
        match[0],
        match[1],
        match[2],
    )


def _served_key(value: Any) -> str | None:
    """A stored key as every reader sees it: text, then canonical Camelot.

    Track.key and the key's provenance both come through here, so the two can
    never disagree (a stored numeric 0 is "0", a no-key sentinel, on both).
    """
    if value is None:
        return None
    return canonical_key_text(value if isinstance(value, str) else str(value))


def _served_field_value(field_name: str, value: Any) -> Any:
    """A field value as readers see it: a stored key in canonical Camelot."""
    return _served_key(value) if field_name == "key" else value


def _tag_year_and_label(year: Any, label: Any) -> tuple[int | None, str | None]:
    """The stored tag year (an int, never a bool) and trimmed label, None when unusable (issue #10029)."""
    clean_year = year if isinstance(year, int) and not isinstance(year, bool) else None
    clean_label = label.strip() or None if isinstance(label, str) else None
    return clean_year, clean_label


def _row_to_track(
    row: sqlite3.Row,
    fields: dict[str, EffectiveField],
) -> Track:
    """Project a ``tracks`` row + its ``track_fields`` entries into ``Track``.

    ``fields`` maps ``field_name -> (value, source, confidence, modified_at)``.
    """
    artist: str | None = None
    artists_json = row["artists_json"]
    if artists_json:
        try:
            decoded = json.loads(artists_json)
            if isinstance(decoded, list) and decoded:
                # Join multi-artist values with ", " for the flat facade.
                artist = ", ".join(str(a) for a in decoded if a)
            elif isinstance(decoded, str):
                artist = decoded
        except (json.JSONDecodeError, TypeError):
            artist = None

    def _val(name: str) -> Any:
        view = fields.get(name)
        return view.value if view is not None else None

    bpm = _val("bpm")
    key = _val("key")
    rating = _val("rating")
    color_id = _val("color_id")
    tags_val = _val("tags")
    notes = _val("notes")
    genre = _val("genre")
    comments = _val("comments")
    last_played_at = _val("last_played_at")
    tempo_pref_val = _val("tempo_pref")

    if isinstance(bpm, (int, float)):
        bpm = float(bpm)
    else:
        bpm = None
    if rating is not None:
        try:
            rating = int(rating)
        except (TypeError, ValueError):
            rating = None
    if color_id is not None:
        try:
            color_id = int(color_id)
        except (TypeError, ValueError):
            color_id = None
    if isinstance(tags_val, list):
        tags = [str(t) for t in tags_val]
    else:
        tags = []
    if notes is not None and not isinstance(notes, str):
        notes = str(notes)
    if genre is not None and not isinstance(genre, str):
        genre = str(genre)
    if comments is not None and not isinstance(comments, str):
        comments = str(comments)
    year, label = _tag_year_and_label(_val("year"), _val("label"))
    if last_played_at is not None and not isinstance(last_played_at, str):
        last_played_at = str(last_played_at)
    # ONE notation on every reader: whatever a source stored ("Am", "1m",
    # "8A"), the track serves canonical Camelot (canon.canonical_key_text).
    key = _served_key(key)
    if isinstance(tempo_pref_val, dict):
        tempo_pref: dict[str, float | None] | None = {
            "regular": tempo_pref_val.get("regular"),
            "min": tempo_pref_val.get("min"),
            "max": tempo_pref_val.get("max"),
        }
    else:
        tempo_pref = None

    provenance: dict[str, Provenance] = {}
    for fname, view in fields.items():
        provenance[fname] = Provenance(
            value=_served_field_value(fname, view.value),
            source=view.source,
            confidence=view.confidence,
            modified_at=view.modified_at,
            status=view.status,
            reason=view.reason,
        )

    return Track(
        stable_id=row["stable_id"],
        title=row["title"],
        artist=artist,
        album=row["album"],
        duration_ms=row["duration_ms"],
        bpm=bpm,
        key=key,
        rating=rating,
        color_id=color_id,
        tags=tags,
        notes=notes,
        genre=genre,
        comments=comments,
        year=year,
        label=label,
        last_played_at=last_played_at,
        tempo_pref=tempo_pref,
        grid_flag_dismissed=_val("grid_flag_dismissed") is True,
        tracklist_hidden=_val("tracklist_hidden") is True,
        auto_gain_user_db=_auto_gain_user_db(_val("auto_gain_user_db")),
        file_path=row["file_path"],
        created_at=row["created_at"],
        updated_at=_effective_updated_at(row["updated_at"], fields),
        selection_tag=_selection_tag(fields),
        provenance=provenance,
    )


def _auto_gain_user_db(value: Any) -> float | None:
    """A stored user auto gain offset, or None (never set, or cleared)."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BackendError(f"stored auto_gain_user_db is not a number: {value!r}")
    return float(value)


def _fetch_fields(
    conn: sqlite3.Connection,
    stable_ids: list[str],
) -> dict[str, dict[str, EffectiveField]]:
    """Fetch the effective projected fields for ``stable_ids``.

    Returns ``{stable_id: {field_name: EffectiveField}}``.

    Two sources, one function. The ordinary EAV fields
    (:data:`_EAV_FIELDS`) come from ``track_fields`` as they always have.
    The LANE-OWNED fields (``bpm``, ``key``, ``loudness_lufs``,
    ``loudness_dbtp``, ``key_change_count``, ``tempo_change_count``) come
    from :func:`apps.analysis.selection.effective_fields`, which reads
    ``track_fields`` for a lane on rbx and ``analysis_projection`` for a
    lane on own. Its answer OVERRIDES the EAV pass, so switching a lane to
    own cannot leave the rekordbox value showing, and an own lane with no
    record shows ``status: missing`` rather than silently falling back --
    which is the substitution native-analysis v1 exists to remove.

    Nothing here writes. An own value never reaches ``track_fields`` or
    ``track_field_history``.
    """
    if not stable_ids:
        return {}
    out: dict[str, dict[str, EffectiveField]] = {sid: {} for sid in stable_ids}
    # Use chunked IN (...) to avoid SQLite's default 999 variable cap.
    chunk = 500
    eav_placeholders = ",".join("?" * len(_EAV_FIELDS))
    for i in range(0, len(stable_ids), chunk):
        sub = stable_ids[i : i + chunk]
        placeholders = ",".join("?" * len(sub))
        sql = (
            f"SELECT stable_id, field_name, value_json, source, confidence, "
            f"       modified_at "
            f"FROM track_fields "
            f"WHERE stable_id IN ({placeholders}) "
            f"  AND field_name IN ({eav_placeholders}) "
            f"  AND deleted_at IS NULL"
        )
        for row in conn.execute(sql, (*sub, *_EAV_FIELDS)):
            sid = row["stable_id"]
            fname = row["field_name"]
            try:
                value = json.loads(row["value_json"])
            except (json.JSONDecodeError, TypeError):
                value = row["value_json"]
            out[sid][fname] = EffectiveField(
                value=value,
                source=row["source"],
                confidence=row["confidence"],
                modified_at=row["modified_at"],
                status="ok",
                reason=None,
            )

    for sid, lane_fields in _lane_owned_fields(conn, stable_ids).items():
        out[sid].update(lane_fields)
    return out


def _matches_track_filter(track: Track, flt: TrackFilter) -> bool:
    """Keep list-track filtering semantics independent of SQLite collation."""
    if flt.q:
        needle = flt.q.casefold()
        if not (
            (track.title and needle in track.title.casefold()) or (track.artist and needle in track.artist.casefold())
        ):
            return False
    if flt.bpm_min is not None and not (track.bpm is not None and track.bpm >= flt.bpm_min):
        return False
    if flt.bpm_max is not None and not (track.bpm is not None and track.bpm <= flt.bpm_max):
        return False
    if flt.keys and track.key not in flt.keys:
        return False
    if flt.rating_min is not None and not (track.rating is not None and track.rating >= flt.rating_min):
        return False
    if flt.tag and flt.tag not in (track.tags or []):
        return False
    if flt.color_ids:
        from .backend import track_matches_color_ids

        if not track_matches_color_ids(track, flt.color_ids):
            return False
    if flt.mytags:
        from .backend import track_matches_mytags

        if not track_matches_mytags(track, flt.mytags, flt.mytag_mode):
            return False
    return True


def _field_writes(current: Track, patch: dict[str, Any]) -> dict[str, Any]:
    """Validate a patch and resolve tag deltas against the current track."""
    writes: dict[str, Any] = {}
    if "rating" in patch:
        rating = patch["rating"]
        if rating is not None and not (0 <= rating <= 5):
            raise BackendError("rating must be between 0 and 5")
        writes["rating"] = rating
    if "color_id" in patch:
        color_id = patch["color_id"]
        if color_id is not None and not (1 <= int(color_id) <= 8):
            raise BackendError("color_id must be between 1 and 8 or null")
        writes["color_id"] = color_id
    if "notes" in patch:
        writes["notes"] = patch["notes"]
    if "genre" in patch:
        writes["genre"] = patch["genre"]
    if "comments" in patch:
        writes["comments"] = patch["comments"]
    if "tempo_pref" in patch:
        writes["tempo_pref"] = resolve_tempo_pref_write(patch["tempo_pref"])
    if "grid_flag_dismissed" in patch:
        writes["grid_flag_dismissed"] = resolve_grid_flag_dismissed_write(patch["grid_flag_dismissed"])
    if "tracklist_hidden" in patch:
        writes["tracklist_hidden"] = resolve_tracklist_hidden_write(patch["tracklist_hidden"])
    if "auto_gain_user_db" in patch:
        writes["auto_gain_user_db"] = resolve_auto_gain_user_db_write(patch["auto_gain_user_db"])
    if "tags_add" in patch or "tags_remove" in patch:
        tags = list(current.tags or [])
        for tag in patch.get("tags_add") or []:
            if tag and tag not in tags:
                tags.append(tag)
        if patch.get("tags_remove"):
            tags = [tag for tag in tags if tag not in patch["tags_remove"]]
        writes["tags"] = tags
    return writes


def _applied_state_in_txn(conn: sqlite3.Connection, stable_ids: list[str]) -> dict[str, AppliedState]:
    """Each written track's ETag and tag set, read on the writing connection BEFORE its COMMIT."""
    out: dict[str, AppliedState] = {}
    for stable_id in stable_ids:
        row = conn.execute(
            "SELECT stable_id, stable_id_tier, title, artists_json, album, isrc, "
            "duration_ms, file_path, content_hash, created_at, updated_at FROM tracks WHERE stable_id = ?",
            (stable_id,),
        ).fetchone()
        out[stable_id] = applied_state_of(_row_to_track(row, _fetch_fields(conn, [stable_id]).get(stable_id, {})))
    return out
