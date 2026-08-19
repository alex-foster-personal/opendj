"""Hot-cue reversal tokens and slot generations (the CAS undo ledger).

Moved verbatim from ``apps/webui/server/rb_vendor.py`` C10 (source lines
1919-2030) per ``.planning/t3b-decomposition-map.md`` section 2 row 11.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException

from .cues import _rb_timestamp


def _ensure_reversal_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS rb_hot_cue_reversal ("
        "ID TEXT PRIMARY KEY, ContentID TEXT NOT NULL, Kind INTEGER NOT NULL, "
        "PreimageJson TEXT, PostRevision TEXT NOT NULL, CreatedAt TEXT NOT NULL, "
        "ConsumedAt TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS rb_hot_cue_slot_revision ("
        "ContentID TEXT NOT NULL, Kind INTEGER NOT NULL, Generation INTEGER NOT NULL, "
        "PRIMARY KEY (ContentID, Kind))"
    )


def _slot_generation(conn: sqlite3.Connection, vendor_id: str, kind: int) -> int:
    _ensure_reversal_tables(conn)
    conn.execute(
        "INSERT OR IGNORE INTO rb_hot_cue_slot_revision (ContentID, Kind, Generation) "
        "VALUES (?, ?, 0)",
        (vendor_id, kind),
    )
    row = conn.execute(
        "SELECT Generation FROM rb_hot_cue_slot_revision WHERE ContentID = ? AND Kind = ?",
        (vendor_id, kind),
    ).fetchone()
    if row is None:
        raise RuntimeError("hot-cue slot generation was not created")
    return int(row[0])


def _bump_slot_generation(conn: sqlite3.Connection, vendor_id: str, kind: int) -> int:
    generation = _slot_generation(conn, vendor_id, kind)
    result = conn.execute(
        "UPDATE rb_hot_cue_slot_revision SET Generation = ? "
        "WHERE ContentID = ? AND Kind = ? AND Generation = ?",
        (generation + 1, vendor_id, kind, generation),
    )
    if result.rowcount != 1:
        raise RuntimeError(
            "hot-cue slot generation changed inside an exclusive transaction"
        )
    return generation + 1


def _create_reversal(
    conn: sqlite3.Connection,
    vendor_id: str,
    kind: int,
    preimage: Mapping[str, Any] | None,
    post_revision: str,
) -> str:
    _ensure_reversal_tables(conn)
    reversal_id = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO rb_hot_cue_reversal "
        "(ID, ContentID, Kind, PreimageJson, PostRevision, CreatedAt, ConsumedAt) "
        "VALUES (?, ?, ?, ?, ?, ?, NULL)",
        (
            reversal_id,
            vendor_id,
            kind,
            json.dumps(preimage, sort_keys=True, separators=(",", ":"))
            if preimage
            else None,
            post_revision,
            _rb_timestamp(),
        ),
    )
    return reversal_id


def _load_reversal(
    conn: sqlite3.Connection,
    reversal_id: str,
    vendor_id: str,
    kind: int,
) -> dict[str, Any] | None:
    _ensure_reversal_tables(conn)
    row = conn.execute(
        "SELECT ContentID, Kind, PreimageJson, PostRevision, ConsumedAt "
        "FROM rb_hot_cue_reversal WHERE ID = ?",
        (reversal_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "HOT_CUE_REVERSAL_NOT_FOUND",
                "message": "unknown reversal token",
            },
        )
    content_id, record_kind, preimage_json, post_revision, consumed_at = row
    if str(content_id) != vendor_id or int(record_kind) != kind:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "HOT_CUE_REVERSAL_SCOPE_CONFLICT",
                "message": "reversal token is bound to another hot-cue slot",
            },
        )
    if consumed_at is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "HOT_CUE_REVERSAL_CONSUMED",
                "message": "reversal token was already consumed",
            },
        )
    return {
        "preimage": json.loads(preimage_json) if preimage_json else None,
        "post_revision": str(post_revision),
    }
