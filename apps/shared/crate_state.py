"""Structured owner-state reconciliation for the agentbox media crate.

The Air remains the owner of library metadata.  Agentbox receives a bounded,
canonical JSON snapshot embedded in the crate manifest and applies it through
``StateWriter``.  The SQLite database itself never crosses the network.

This module is deliberately separate from the ordinary web UI backend: local
mode never imports or calls it.  It exists only at the crate-sync boundary.

Moved from ``(private path)`` (issue #5639): it is pure snapshot,
digest and apply code with no agentbox runtime, and ``apps/webui/crate_sync.py``
imports it, so it lives in ``apps/shared`` where every tree carries it.
Supersedes: (private path) (deleted, no shim).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from apps.shared.state import db as state_db_mod
from apps.shared.state.provenance import WRAPPED_FIELDS
from apps.shared.state.writer import StateWriter

SNAPSHOT_VERSION = 1
_TRACK_COLUMNS = (
    "stable_id",
    "stable_id_tier",
    "title",
    "artists_json",
    "album",
    "isrc",
    "duration_ms",
    "file_path",
    "content_hash",
)


def _canonical_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def snapshot_digest(snapshot: dict[str, object]) -> str:
    """Digest the semantic library ledger, independent of JSON formatting."""
    return hashlib.sha256(_canonical_bytes(snapshot)).hexdigest()


@contextmanager
def _read_state_db(state_db: Path) -> Iterator[sqlite3.Connection]:
    conn = state_db_mod.open_ro(state_db)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _chunks(values: Sequence[str], size: int = 500) -> list[tuple[str, ...]]:
    return [tuple(values[index : index + size]) for index in range(0, len(values), size)]


def _rows_for_ids(
    conn: sqlite3.Connection,
    *,
    table: str,
    id_column: str,
    ids: Sequence[str],
    columns: str,
    order_by: str,
) -> list[sqlite3.Row]:
    """Every synced table this helper reads (tracks, track_vendor_ids,
    track_fields, playlist_memberships) carries ``deleted_at``; a tombstoned
    row must not enter the snapshot exported to the agentbox hub (ADR 08
    point 5), so the filter is unconditional here rather than per-caller.
    """
    rows: list[sqlite3.Row] = []
    for chunk in _chunks(ids):
        marks = ",".join("?" for _ in chunk)
        rows.extend(
            conn.execute(
                f"SELECT {columns} FROM {table} WHERE {id_column} IN ({marks}) "
                f"AND deleted_at IS NULL ORDER BY {order_by}",
                chunk,
            ).fetchall()
        )
    return rows


def _playlist_rows(conn: sqlite3.Connection, *, playlist_name: str | None) -> list[sqlite3.Row]:
    if playlist_name is None:
        return conn.execute(
            "SELECT playlist_id, name, vendor, vendor_pl_id FROM playlists "
            "WHERE deleted_at IS NULL ORDER BY playlist_id"
        ).fetchall()
    rows = conn.execute(
        "SELECT playlist_id, name, vendor, vendor_pl_id FROM playlists "
        "WHERE name = ? AND deleted_at IS NULL ORDER BY playlist_id",
        (playlist_name,),
    ).fetchall()
    if not rows:
        raise RuntimeError(f"playlist {playlist_name!r} is absent from owner state")
    return rows


def _memberships(conn: sqlite3.Connection, playlist_ids: Sequence[str]) -> dict[str, list[str]]:
    result = {playlist_id: [] for playlist_id in playlist_ids}
    for row in _rows_for_ids(
        conn,
        table="playlist_memberships",
        id_column="playlist_id",
        ids=playlist_ids,
        columns="playlist_id, stable_id, position",
        order_by="playlist_id, position",
    ):
        result[str(row["playlist_id"])].append(str(row["stable_id"]))
    return result


def _expected_track_index(
    expected: dict[str, object] | None,
) -> dict[str, dict[str, object]]:
    """Index the owner ledger by stable_id so replica reads can be filtered."""
    if expected is None:
        return {}
    return {
        str(item["stable_id"]): item
        for item in expected.get("tracks", [])  # type: ignore[union-attr]
        if isinstance(item, dict) and "stable_id" in item
    }


def _vendor_ids_by_track(
    vendor_rows: Sequence[sqlite3.Row],
    ordered_ids: Sequence[str],
    expected_tracks: dict[str, dict[str, object]],
    *,
    filtered: bool,
) -> dict[str, list[dict[str, object]]]:
    vendors: dict[str, list[dict[str, object]]] = {sid: [] for sid in ordered_ids}
    for row in vendor_rows:
        sid = str(row["stable_id"])
        item = {"vendor": str(row["vendor"]), "vendor_id": str(row["vendor_id"])}
        if filtered and item not in expected_tracks.get(sid, {}).get("vendor_ids", []):
            continue
        vendors[sid].append(item)
    return vendors


def _expected_field_names(expected_tracks: dict[str, dict[str, object]], stable_id: str) -> set[str]:
    return {
        str(field["field_name"])
        for field in expected_tracks.get(stable_id, {}).get("fields", [])
        if isinstance(field, dict) and "field_name" in field
    }


def _fields_by_track(
    field_rows: Sequence[sqlite3.Row],
    ordered_ids: Sequence[str],
    expected_tracks: dict[str, dict[str, object]],
    *,
    filtered: bool,
) -> dict[str, list[dict[str, object]]]:
    fields: dict[str, list[dict[str, object]]] = {sid: [] for sid in ordered_ids}
    for row in field_rows:
        sid = str(row["stable_id"])
        if str(row["field_name"]) not in WRAPPED_FIELDS:
            continue
        item: dict[str, object] = {
            "field_name": str(row["field_name"]),
            "value": json.loads(str(row["value_json"])),
            "source": str(row["source"]),
            "modified_at": str(row["modified_at"]),
        }
        if row["confidence"] is not None:
            item["confidence"] = float(row["confidence"])
        if filtered and item["field_name"] not in _expected_field_names(expected_tracks, sid):
            continue
        fields[sid].append(item)
    return fields


def _track_records(
    track_rows: Sequence[sqlite3.Row],
    vendors: dict[str, list[dict[str, object]]],
    fields: dict[str, list[dict[str, object]]],
) -> list[dict[str, object]]:
    tracks: list[dict[str, object]] = []
    for row in track_rows:
        sid = str(row["stable_id"])
        artists = json.loads(str(row["artists_json"] or "[]"))
        if not isinstance(artists, list) or not all(isinstance(artist, str) for artist in artists):
            raise RuntimeError(f"track {sid} has malformed artists_json")
        tracks.append(
            {
                "stable_id": sid,
                "stable_id_tier": str(row["stable_id_tier"]),
                "title": row["title"],
                "artists": artists,
                "album": row["album"],
                "isrc": row["isrc"],
                "duration_ms": row["duration_ms"],
                "file_path": row["file_path"],
                "content_hash": row["content_hash"],
                "vendor_ids": vendors[sid],
                "fields": fields[sid],
            }
        )
    return tracks


def _snapshot_from_connection(
    conn: sqlite3.Connection,
    *,
    track_ids: Sequence[str],
    playlist_rows: Sequence[sqlite3.Row],
    expected: dict[str, object] | None = None,
) -> dict[str, object]:
    """Read a deterministic subset; when auditing, ignore replica-only fields."""
    ordered_ids = tuple(sorted(set(track_ids)))
    track_rows = _rows_for_ids(
        conn,
        table="tracks",
        id_column="stable_id",
        ids=ordered_ids,
        columns=", ".join(_TRACK_COLUMNS),
        order_by="stable_id",
    )
    vendor_rows = _rows_for_ids(
        conn,
        table="track_vendor_ids",
        id_column="stable_id",
        ids=ordered_ids,
        columns="stable_id, vendor, vendor_id",
        order_by="stable_id, vendor",
    )
    field_rows = _rows_for_ids(
        conn,
        table="track_fields",
        id_column="stable_id",
        ids=ordered_ids,
        columns=("stable_id, field_name, value_json, source, confidence, modified_at"),
        order_by="stable_id, field_name",
    )

    filtered = expected is not None
    expected_tracks = _expected_track_index(expected)
    vendors = _vendor_ids_by_track(vendor_rows, ordered_ids, expected_tracks, filtered=filtered)
    fields = _fields_by_track(field_rows, ordered_ids, expected_tracks, filtered=filtered)
    tracks = _track_records(track_rows, vendors, fields)

    playlist_ids = [str(row["playlist_id"]) for row in playlist_rows]
    members = _memberships(conn, playlist_ids)
    playlists = [
        {
            "playlist_id": str(row["playlist_id"]),
            "name": str(row["name"]),
            "vendor": str(row["vendor"]),
            "vendor_pl_id": str(row["vendor_pl_id"]),
            "stable_ids": members[str(row["playlist_id"])],
        }
        for row in sorted(playlist_rows, key=lambda item: str(item["playlist_id"]))
    ]
    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "tracks": tracks,
        "playlists": playlists,
    }


def build_snapshot(
    state_db: Path,
    *,
    playlist_name: str | None = None,
    stable_ids: Sequence[str] | None = None,
) -> dict[str, object]:
    """Export selected owner state as records, never as SQLite bytes."""
    if playlist_name is not None and stable_ids is not None:
        raise RuntimeError("playlist_name and stable_ids are mutually exclusive")
    with _read_state_db(state_db) as conn:
        playlists = _playlist_rows(conn, playlist_name=playlist_name)
        if playlist_name is not None:
            ids = sorted(
                {
                    sid
                    for members in _memberships(conn, [str(row["playlist_id"]) for row in playlists]).values()
                    for sid in members
                }
            )
        elif stable_ids is not None:
            playlists = []
            ids = sorted(set(stable_ids))
        else:
            ids = [str(row[0]) for row in conn.execute("SELECT stable_id FROM tracks WHERE deleted_at IS NULL")]
        snapshot = _snapshot_from_connection(conn, track_ids=ids, playlist_rows=playlists)
    found = {str(item["stable_id"]) for item in snapshot["tracks"]}  # type: ignore[index]
    missing = sorted(set(ids) - found)
    if missing:
        raise RuntimeError("owner state lacks selected tracks: " + ", ".join(missing))
    return snapshot


def _validate_track(track: object, seen: set[str]) -> str:
    """Check one ledger track and return its stable_id, or raise."""
    if not isinstance(track, dict):
        raise TypeError("crate library track must be an object")
    sid = track.get("stable_id")
    if not isinstance(sid, str) or not sid or sid in seen:
        raise RuntimeError("crate library stable_ids must be unique strings")
    artists = track.get("artists")
    if not isinstance(artists, list) or not all(isinstance(artist, str) for artist in artists):
        raise RuntimeError(f"crate library track {sid} has invalid artists")
    return sid


def _validate_playlist(playlist: object, ids: set[str]) -> None:
    """Check one ledger playlist against the track ids the ledger declares."""
    if not isinstance(playlist, dict) or not isinstance(playlist.get("stable_ids"), list):
        raise TypeError("crate library playlist must include stable_ids")
    missing = set(playlist["stable_ids"]) - ids
    if missing:
        raise RuntimeError(
            f"crate playlist {playlist.get('name')!r} references absent tracks: "
            + ", ".join(sorted(str(item) for item in missing))
        )


def _validate_snapshot(snapshot: dict[str, object]) -> None:
    if snapshot.get("snapshot_version") != SNAPSHOT_VERSION:
        raise RuntimeError("unsupported crate library snapshot version")
    tracks = snapshot.get("tracks")
    playlists = snapshot.get("playlists")
    if not isinstance(tracks, list) or not isinstance(playlists, list):
        raise TypeError("crate library snapshot needs tracks and playlists lists")
    ids: set[str] = set()
    for track in tracks:
        ids.add(_validate_track(track, ids))
    for playlist in playlists:
        _validate_playlist(playlist, ids)


def _expected_rows(conn: sqlite3.Connection, snapshot: dict[str, object]) -> dict[str, object]:
    track_ids = [str(item["stable_id"]) for item in snapshot["tracks"]]  # type: ignore[index]
    playlist_ids = [
        str(item["playlist_id"])
        for item in snapshot["playlists"]  # type: ignore[index]
    ]
    playlists = _rows_for_ids(
        conn,
        table="playlists",
        id_column="playlist_id",
        ids=playlist_ids,
        columns="playlist_id, name, vendor, vendor_pl_id",
        order_by="playlist_id",
    )
    return _snapshot_from_connection(
        conn,
        track_ids=track_ids,
        playlist_rows=playlists,
        expected=snapshot,
    )


def audit_snapshot(state_db: Path, snapshot: dict[str, object]) -> dict[str, object]:
    _validate_snapshot(snapshot)
    with _read_state_db(state_db) as conn:
        actual = _expected_rows(conn, snapshot)
    expected_digest = snapshot_digest(snapshot)
    actual_digest = snapshot_digest(actual)
    return {
        "ok": expected_digest == actual_digest,
        "expected_digest": expected_digest,
        "actual_digest": actual_digest,
        "expected_tracks": len(snapshot["tracks"]),  # type: ignore[arg-type]
        "actual_tracks": len(actual["tracks"]),  # type: ignore[arg-type]
        "expected_playlists": len(snapshot["playlists"]),  # type: ignore[arg-type]
        "actual_playlists": len(actual["playlists"]),  # type: ignore[arg-type]
    }


def _backup_state_db(state_db: Path, backup_dir: Path, digest: str) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_dir / f"state-before-crate-{stamp}-{digest[:12]}.db"
    source = sqlite3.connect(str(state_db))
    destination = sqlite3.connect(str(backup))
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return backup


def _apply_track(conn: sqlite3.Connection, writer: StateWriter, track: dict[str, object]) -> int:
    """Write one owner track record and return how many rows it changed."""
    changed = int(
        writer.upsert_track(
            stable_id=str(track["stable_id"]),
            stable_id_tier=str(track["stable_id_tier"]),
            title=track.get("title"),
            artists=track["artists"],
            album=track.get("album"),
            isrc=track.get("isrc"),
            duration_ms=track.get("duration_ms"),
            file_path=track.get("file_path"),
            content_hash=track.get("content_hash"),
        )
    )
    sid = str(track["stable_id"])
    for vendor in track.get("vendor_ids", []):
        current = conn.execute(
            "SELECT vendor_id FROM track_vendor_ids WHERE stable_id = ? AND vendor = ?",
            (sid, vendor["vendor"]),
        ).fetchone()
        if current is None or str(current[0]) != str(vendor["vendor_id"]):
            writer.set_vendor_id(sid, str(vendor["vendor"]), str(vendor["vendor_id"]))
            changed += 1
    for field in track.get("fields", []):
        changed += int(
            writer.set_field(
                sid,
                str(field["field_name"]),
                field["value"],
                source=field["source"],
                modified_at=str(field["modified_at"]),
                confidence=field.get("confidence"),
            )
        )
    return changed


def _apply_playlist(conn: sqlite3.Connection, writer: StateWriter, playlist: dict[str, object]) -> int:
    """Write one owner playlist and its ordering, returning rows changed."""
    playlist_id = str(playlist["playlist_id"])
    existing = conn.execute(
        "SELECT vendor, vendor_pl_id FROM playlists WHERE playlist_id = ?",
        (playlist_id,),
    ).fetchone()
    if existing is not None and tuple(existing) != (
        str(playlist["vendor"]),
        str(playlist["vendor_pl_id"]),
    ):
        raise RuntimeError(f"playlist identity collision for {playlist_id}")
    changed = int(
        writer.insert_playlist(
            playlist_id=playlist_id,
            name=str(playlist["name"]),
            vendor=str(playlist["vendor"]),
            vendor_pl_id=str(playlist["vendor_pl_id"]),
        )
    )
    wanted = [str(sid) for sid in playlist["stable_ids"]]
    current = [
        str(row[0])
        for row in conn.execute(
            "SELECT stable_id FROM playlist_memberships WHERE playlist_id = ? AND deleted_at IS NULL ORDER BY position",
            (playlist_id,),
        )
    ]
    if current != wanted:
        writer.set_playlist_memberships(playlist_id, wanted)
        changed += 1
    return changed


def reconcile_snapshot(
    state_db: Path,
    snapshot: dict[str, object],
    *,
    backup_dir: Path,
) -> dict[str, object]:
    """Apply a bounded owner snapshot transactionally and prove it afterward."""
    _validate_snapshot(snapshot)
    before = audit_snapshot(state_db, snapshot)
    if before["ok"] is True:
        return {"ok": True, "changed": 0, "backup": None, **before}

    expected_digest = snapshot_digest(snapshot)
    backup = _backup_state_db(state_db, backup_dir, expected_digest)
    conn = state_db_mod.open_rw(state_db)
    conn.row_factory = sqlite3.Row
    writer = StateWriter(conn, actor="agentbox.crate-sync")
    changed = 0
    try:
        with writer.transaction():
            for track in snapshot["tracks"]:  # type: ignore[union-attr]
                assert isinstance(track, dict)
                changed += _apply_track(conn, writer, track)

            for playlist in snapshot["playlists"]:  # type: ignore[union-attr]
                assert isinstance(playlist, dict)
                changed += _apply_playlist(conn, writer, playlist)

            actual = _expected_rows(conn, snapshot)
            actual_digest = snapshot_digest(actual)
            if actual_digest != expected_digest:
                raise RuntimeError(
                    "replica library ledger mismatch after state reconciliation: "
                    f"expected {expected_digest}, got {actual_digest}"
                )
    finally:
        writer.close()
        conn.close()

    after = audit_snapshot(state_db, snapshot)
    return {"changed": changed, "backup": str(backup), **after}


def snapshot_from_manifest(manifest: dict[str, object]) -> dict[str, object]:
    snapshot = manifest.get("library")
    if not isinstance(snapshot, dict):
        raise TypeError("crate manifest has no structured library ledger")
    expected = manifest.get("library_digest")
    actual = snapshot_digest(snapshot)
    if expected != actual:
        raise RuntimeError(f"crate library ledger digest mismatch: expected {expected}, got {actual}")
    _validate_snapshot(snapshot)
    return snapshot


def normalise_manifest(manifest: dict[str, object]) -> dict[str, object]:
    """Bound an owner ledger to fields the production writer can reproduce.

    Older schema-v3 producers may include analysis-only EAV rows.  Verify that
    original ledger first, then deterministically filter it and rotate the
    digest before any replica write.  This is not a silent fallback: the
    original digest remains recorded as ``source_library_digest``.
    """
    snapshot = snapshot_from_manifest(manifest)
    normalised = json.loads(json.dumps(snapshot, ensure_ascii=False))
    for track in normalised["tracks"]:
        track["fields"] = [field for field in track.get("fields", []) if field.get("field_name") in WRAPPED_FIELDS]
    digest = snapshot_digest(normalised)
    if digest != manifest.get("library_digest"):
        manifest["source_library_digest"] = manifest["library_digest"]
        manifest["library"] = normalised
        manifest["library_digest"] = digest
    return manifest


__all__ = [
    "SNAPSHOT_VERSION",
    "audit_snapshot",
    "build_snapshot",
    "normalise_manifest",
    "reconcile_snapshot",
    "snapshot_digest",
    "snapshot_from_manifest",
]
