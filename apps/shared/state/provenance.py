"""Provenance envelope writer + reader + open-dj Track projection.

Rules (open-dj v0 strawman section 4.1 + section 6):

* Analysed fields (``bpm``, ``key``, ``energy``, ``rating``, plus the
  reserved ``cue_points`` / ``beatgrid`` blobs) are provenance-wrapped.
  Writes land in ``track_fields``; the previous value is moved to
  ``track_field_history`` before overwrite (idempotent on byte-equal
  value+source+modified_at).
* Identity facts (``title``, ``artists``, ``album``, ``isrc``,
  ``duration_ms``, ``file_path``, ``content_hash``) live on ``tracks`` and
  are NOT wrapped.
* ``value_json`` is written with ``sort_keys=True`` + compact separators
  so two identical values are byte-stable.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from . import sync_stamp as _sync_stamp
from .types import SOURCES, ProvenanceValue, Source

# Whitelist of field_names allowed in track_fields. Everything else must
# live as an identity column on tracks. cue_points / beatgrid_* are
# blob-shaped for Phase 5; Phase 15 may split them into row-per-anchor.
WRAPPED_FIELDS: frozenset[str] = frozenset(
    {
        "bpm",
        "key",
        "energy",
        # Integrated loudness as the source reports it. Units are NOT
        # interchangeable between sources (dBFS vs RMS vs LUFS integrated vs
        # LUFS short-term are four numbers wearing one word), so a loudness
        # write must clear the equivalence gate first -- see
        # apps/shared/equivalence.py and docs/analysis-retention.md.
        "loudness",
        # Clipped-peak count as the analyser observed it (MIK
        # ZSONG.ZCLIPPEDPEAKCOUNT: 2,379 of 7,026 tracks carry at least one
        # clipped peak, max 88,680). Free QC data, 100% incremental.
        "clipped_peak_count",
        "rating",
        "cue_points",
        "beatgrid",
        "beatgrid_anchors",
        "tempo_segments",
        # webui-editable annotations (PATCH /tracks/{stable_id}). The webui
        # daemon persists these through StateWriter.set_field with
        # source='webui', confidence=1.0 so edits survive restart.
        "notes",
        "tags",
        "genre",
        "comments",
        # PREF-01: {"regular": float|None, "min": float|None, "max": float|None}.
        "tempo_pref",
        # GRIDFLAG-04: true while the user has hidden this track's beatgrid
        # flag ("I know, stop telling me"). A bool, set from the Err column or
        # PUT /beatgrid-flags/{id}/dismissed.
        "grid_flag_dismissed",
    }
)


# ---------- helpers -----------------------------------------------------


def _canonical_json(value: Any) -> str:
    """Canonical JSON encoding: sorted keys, compact separators, UTF-8."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _validate_rfc3339_utc(raw: str) -> None:
    """Raise ValueError if ``raw`` is not an RFC-3339 UTC timestamp.

    We require the ``T`` date/time separator + either ``Z`` or ``+00:00``
    so that every ``modified_at`` sorts lexicographically (open-dj §4.1
    leans on this for history ordering).
    """
    if not isinstance(raw, str) or not raw:
        raise ValueError("modified_at must be a non-empty RFC 3339 UTC string")
    if "T" not in raw:
        raise ValueError(f"not RFC 3339 (missing 'T' separator): {raw!r}")
    parseable = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(parseable)
    except ValueError as exc:
        raise ValueError(f"not RFC 3339: {raw!r}") from exc
    if dt.tzinfo is None or dt.utcoffset().total_seconds() != 0:  # type: ignore[union-attr]
        raise ValueError(
            f"modified_at must be UTC (got offset={dt.utcoffset()!r})"
        )


def _validate_field(field_name: str, source: str) -> None:
    if field_name not in WRAPPED_FIELDS:
        raise ValueError(
            f"field_name {field_name!r} is not in WRAPPED_FIELDS "
            f"{sorted(WRAPPED_FIELDS)}"
        )
    if source not in SOURCES:
        raise ValueError(
            f"source {source!r} is not in SOURCES {sorted(SOURCES)}"
        )


# ---------- core writer -------------------------------------------------


def write_field(
    conn: sqlite3.Connection,
    *,
    stable_id: str,
    field_name: str,
    value: Any,
    source: Source,
    modified_at: str,
    confidence: float | None = None,
    actor: str | None = None,
    now: str | None = None,
    machine_id: str | None = None,
) -> bool:
    """Upsert a provenance-wrapped field.

    Returns ``True`` if the row was inserted/updated, ``False`` if the
    write was a byte-stable no-op (same value_json + source + confidence +
    modified_at). Idempotent writes do not append to history or events.

    Runs inside an IMMEDIATE transaction. Appends one row to ``events``
    on a real change. On overwrite, the prior row is copied to
    ``track_field_history`` with ``superseded_at = now``.

    ``track_fields`` is a synced table, so a real change is stamped through
    :func:`apps.shared.state.sync_stamp.stamp_and_log`. Round 1 finding A1
    was this write leaving ``updated_at`` NULL: every edit after the first
    then presented the same LWW key, was rejected by the hub, and bricked
    the machine on the next digest compare. ``machine_id`` defaults to the
    local machine, resolved from the connection's data dir.
    """
    _validate_field(field_name, source)
    _validate_rfc3339_utc(modified_at)
    if confidence is not None and not (0.0 <= confidence <= 1.0):
        raise ValueError(f"confidence must be in [0, 1], got {confidence!r}")

    value_json = _canonical_json(value)
    # One clock for the whole call: history's superseded_at, the sync stamp
    # and the events row all read the same instant, in the one canonical
    # format the LWW comparison can order (ADR 08 point 2).
    stamp_at = _sync_stamp.canonical_now() if now is None else _sync_stamp.to_canonical(now)

    # Per-call SAVEPOINT so nested transactions (e.g. a dry-run ingest
    # adapter wrapping the writer in its own SAVEPOINT) compose cleanly.
    # SQLite identifier rules: keep the name alphanumeric + underscore.
    sp_name = f"prov_{id(conn)}_{abs(hash((stable_id, field_name, stamp_at)))}"
    conn.execute(f"SAVEPOINT {sp_name}")
    try:
        existing = conn.execute(
            "SELECT value_json, source, confidence, modified_at "
            "FROM track_fields WHERE stable_id = ? AND field_name = ?",
            (stable_id, field_name),
        ).fetchone()
        if existing is not None:
            if (
                existing[0] == value_json
                and existing[1] == source
                and existing[2] == confidence
                and existing[3] == modified_at
            ):
                conn.execute(f"RELEASE SAVEPOINT {sp_name}")
                return False
            conn.execute(
                "INSERT INTO track_field_history(stable_id, field_name, "
                "value_json, source, confidence, modified_at, superseded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    stable_id,
                    field_name,
                    existing[0],
                    existing[1],
                    existing[2],
                    existing[3],
                    stamp_at,
                ),
            )
        stamp = _sync_stamp.stamp_and_log(
            conn,
            "track_fields",
            (stable_id, field_name),
            machine_id or _sync_stamp.ensure_local_machine(conn),
            now=stamp_at,
        )
        conn.execute(
            "INSERT INTO track_fields(stable_id, field_name, "
            "value_json, source, confidence, modified_at, updated_at, "
            "origin_device_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(stable_id, field_name) DO UPDATE SET "
            "value_json=excluded.value_json, source=excluded.source, "
            "confidence=excluded.confidence, "
            "modified_at=excluded.modified_at, "
            "updated_at=excluded.updated_at, "
            "origin_device_id=excluded.origin_device_id",
            (
                stable_id,
                field_name,
                value_json,
                source,
                confidence,
                modified_at,
                stamp.updated_at,
                stamp.origin_device_id,
            ),
        )
        payload = {
            "field_name": field_name,
            "value": value,
            "source": source,
            "modified_at": modified_at,
        }
        if confidence is not None:
            payload["confidence"] = confidence
        conn.execute(
            "INSERT INTO events(ts, kind, stable_id, payload_json, actor) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                stamp_at,
                "track.field.set",
                stable_id,
                _canonical_json(payload),
                actor,
            ),
        )
        conn.execute(f"RELEASE SAVEPOINT {sp_name}")
    except Exception:
        conn.execute(f"ROLLBACK TO SAVEPOINT {sp_name}")
        conn.execute(f"RELEASE SAVEPOINT {sp_name}")
        raise
    return True


def read_field(
    conn: sqlite3.Connection, stable_id: str, field_name: str
) -> ProvenanceValue | None:
    """Return the current ProvenanceValue for ``(stable_id, field_name)`` or None."""
    row = conn.execute(
        "SELECT value_json, source, confidence, modified_at "
        "FROM track_fields WHERE stable_id = ? AND field_name = ?",
        (stable_id, field_name),
    ).fetchone()
    if row is None:
        return None
    value = json.loads(row[0])
    return ProvenanceValue(
        value=value, source=row[1], confidence=row[2], modified_at=row[3]
    )


def read_history(
    conn: sqlite3.Connection, stable_id: str, field_name: str
) -> list[ProvenanceValue]:
    """Return the history for one field, newest-superseded first."""
    rows = conn.execute(
        "SELECT value_json, source, confidence, modified_at "
        "FROM track_field_history "
        "WHERE stable_id = ? AND field_name = ? "
        "ORDER BY superseded_at DESC, id DESC",
        (stable_id, field_name),
    ).fetchall()
    return [
        ProvenanceValue(
            value=json.loads(r[0]), source=r[1], confidence=r[2], modified_at=r[3]
        )
        for r in rows
    ]


# ---------- open-dj Track projection -----------------------------------


def to_open_dj_track(
    conn: sqlite3.Connection, stable_id: str
) -> dict[str, Any] | None:
    """Project one row into open-dj v0 strawman section 4.2 shape.

    Returns None if the stable_id is unknown. Output dict uses sorted keys
    (canonical form) so callers can diff / JCS-sign downstream.
    """
    track = conn.execute(
        "SELECT title, artists_json, album, isrc, duration_ms, file_path, "
        "content_hash, stable_id_tier, created_at, updated_at "
        "FROM tracks WHERE stable_id = ?",
        (stable_id,),
    ).fetchone()
    if track is None:
        return None
    (
        title,
        artists_json,
        album,
        isrc,
        duration_ms,
        file_path,
        content_hash,
        stable_id_tier,
        created_at,
        updated_at,
    ) = track

    try:
        artists = json.loads(artists_json) if artists_json else []
    except json.JSONDecodeError:
        artists = []

    # Provenance-wrapped fields.
    wrapped = {}
    for row in conn.execute(
        "SELECT field_name, value_json, source, confidence, modified_at "
        "FROM track_fields WHERE stable_id = ?",
        (stable_id,),
    ):
        field_name, value_json, source, confidence, modified_at = row
        pv = ProvenanceValue(
            value=json.loads(value_json),
            source=source,
            confidence=confidence,
            modified_at=modified_at,
        )
        wrapped[field_name] = pv.as_open_dj()

    # Vendor IDs.
    vendor_ids: dict[str, str] = {}
    for vendor, vendor_id in conn.execute(
        "SELECT vendor, vendor_id FROM track_vendor_ids WHERE stable_id = ?",
        (stable_id,),
    ):
        vendor_ids[vendor] = vendor_id

    doc: dict[str, Any] = {
        "track_id": stable_id,
        "stable_id_tier": stable_id_tier,
        "title": title,
        "artists": artists,
        "album": album,
        "isrc": isrc,
        "duration_ms": duration_ms,
        "file_path": file_path,
        "content_hash": content_hash,
        "vendor_ids": vendor_ids,
        "created_at": created_at,
        "updated_at": updated_at,
    }
    doc.update(wrapped)
    return doc


__all__ = [
    "WRAPPED_FIELDS",
    "read_field",
    "read_history",
    "to_open_dj_track",
    "write_field",
]
