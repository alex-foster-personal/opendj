"""Row readers and payload builders shared by the enrollment test modules.

Plain functions, no fixtures -- the fixtures live in ``conftest.py`` where
pytest finds them without an import. These are here so the mechanism tests
and the CLI tests read the SAME rows the same way: a parity test that built
its own reader would be comparing its own two readers as much as the two code
paths under test.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from apps.shared.state import db as state_db
from apps.shared.state import machine_identity, sync_stamp
from apps.shared.state import schema as state_schema
from apps.sync_hub import client, maintenance_enroll

from .conftest import ENROLL_OWNER_EMAIL, ENROLL_PATH
from .enrollment_transport import TestClientTransport


def read_hub(hub_dir: Path, table: str) -> list[dict[str, Any]]:
    """Every row of ``table`` on the hub, as dicts, ordered deterministically.

    ``SELECT *`` on purpose: a parity comparison has to see EVERY column,
    including one added later. A hand-written column list would silently stop
    comparing whatever the next migration adds.
    """
    conn = state_db.open_rw(client.state_db_path(hub_dir))
    try:
        conn.row_factory = sqlite3.Row
        order = "machine_id" if table != "enrollment_grants" else "grant_token_sha256"
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")]
    finally:
        conn.close()


def owner_rows(hub_dir: Path) -> list[dict[str, Any]]:
    return read_hub(hub_dir, "machine_owners")


def hub_machine_id(hub_dir: Path) -> str:
    return machine_identity.get_or_create_machine_id(hub_dir)


def mint_grant(
    hub_dir: Path, *, email: str = ENROLL_OWNER_EMAIL, ttl_s: int = 900
) -> str:
    """Mint through the same function the CLI's ``grant`` subcommand calls."""
    return maintenance_enroll.grant(hub_dir, owner_email=email, ttl_s=ttl_s).token


def machine_payload(
    data_dir: Path, *, name: str, is_hub: bool = False
) -> dict[str, Any]:
    stamp = sync_stamp.canonical_now()
    return {
        "machine_id": machine_identity.get_or_create_machine_id(data_dir),
        "name": name,
        "platform": machine_identity.detect_platform(),
        "is_hub": is_hub,
        "data_root": str(data_dir),
        "first_seen": stamp,
        "last_seen": stamp,
    }


def http_enroll(
    hub: TestClientTransport,
    data_dir: Path,
    *,
    name: str,
    token: str,
    kind: str = "grant",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """POST the enroll endpoint directly, bypassing the CLI entirely."""
    body: dict[str, Any] = {
        "machine": machine_payload(data_dir, name=name),
        "schema_version": state_schema.SCHEMA_VERSION,
        "credential": {"kind": kind, "value": token},
    }
    body.update(extra or {})
    return hub.post(ENROLL_PATH, body)


def masked_owner(row: dict[str, Any]) -> dict[str, Any]:
    """Everything about an owner row that must NOT depend on who wrote it."""
    return {k: v for k, v in row.items() if k not in ("machine_id", "enrolled_at")}


def masked_machine(row: dict[str, Any]) -> dict[str, Any]:
    """The per-machine facts two DIFFERENT machines still have in common."""
    return {
        k: v
        for k, v in row.items()
        if k not in ("machine_id", "name", "data_root", "first_seen", "last_seen")
    }


__all__ = [
    "http_enroll",
    "hub_machine_id",
    "machine_payload",
    "masked_machine",
    "masked_owner",
    "mint_grant",
    "owner_rows",
    "read_hub",
]
