"""Hot-cue reversal tokens and slot generations (the CAS undo ledger).

Moved from ``apps/webui/server/rb_vendor.py`` C10 (source lines 1919-2030) per
``.planning/t3b-decomposition-map.md`` section 2 row 11.

D2 (map section 3): ``_ensure_reversal_tables`` was **deleted** rather than
moved. ``apps/engine_core/store/schema.py`` already carried byte-identical DDL
as ``VENDOR_SIDECAR_DDL`` / :func:`ensure_vendor_sidecar_tables`, commented
"declared here so the DDL has one home"; the canonical home had moved but the
deletion never happened, so a drift test existed only to hold the two copies
level. Its four callers now provision through the schema module, and that
drift test is deleted alongside the copy it was watching -- with one home
there is nothing left to drift.

Wave 4 (S8) moved this module out of ``apps/webui/server/rb_vendor_pkg/``. The
``apps.engine_core.store.schema`` import below is the edge that closed the
``engine_core <-> webui`` package cycle while this module lived under
``apps.webui``; raised from ``apps.adapters`` it closes no loop. The body is
unchanged.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

from fastapi import HTTPException

from apps.engine_core.store.schema import ensure_vendor_sidecar_tables

from .cues import _rb_timestamp

_SLOT_REVISION_TABLE = "rb_hot_cue_slot_revision"

# An unprovisioned slot and a slot provisioned at generation 0 are the same
# state: `_slot_generation` INSERTs `Generation = 0` and hands back 0. So the
# read path can report 0 for a missing row, produce a byte-identical CAS
# revision, and never take a write transaction (D5).
_UNPROVISIONED_GENERATION = 0


def _slot_revision_table_exists(conn: sqlite3.Connection) -> bool:
    """True once any write path has provisioned the sidecar.

    A vendor DB never written through this surface simply has no sidecar
    tables yet. That is a legitimate state with a defined answer -- every slot
    at generation 0 -- not a missing table to paper over.
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (_SLOT_REVISION_TABLE,),
    ).fetchone()
    return row is not None


def _read_slot_generations(
    conn: sqlite3.Connection,
    vendor_id: str,
    kinds: Iterable[int],
) -> dict[int, int]:
    """Slot generations for ``kinds``, provisioning none of them.

    Read-only counterpart to :func:`_slot_generation`, and the reason
    ``GET /tracks/{id}/hot-cues`` no longer writes. One query for the whole
    bank instead of one INSERT-then-SELECT per slot.
    """
    wanted = list(kinds)
    if not _slot_revision_table_exists(conn):
        return dict.fromkeys(wanted, _UNPROVISIONED_GENERATION)
    rows = conn.execute(
        f"SELECT Kind, Generation FROM {_SLOT_REVISION_TABLE} WHERE ContentID = ?",
        (vendor_id,),
    ).fetchall()
    stored = {int(kind): int(generation) for kind, generation in rows}
    return {kind: stored.get(kind, _UNPROVISIONED_GENERATION) for kind in wanted}


def _slot_generation(conn: sqlite3.Connection, vendor_id: str, kind: int) -> int:
    ensure_vendor_sidecar_tables(conn)
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
    ensure_vendor_sidecar_tables(conn)
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
    ensure_vendor_sidecar_tables(conn)
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
