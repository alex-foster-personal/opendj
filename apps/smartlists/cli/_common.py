"""Shared CLI helpers -- repo construction and smartlist JSON snapshots."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from apps.shared.smartlists import SmartlistRow
from apps.shared.state.phase08_open import open_phase08_state_db
from apps.smartlists.repo import SmartlistsRepo, smartlist_revision


def read_rule_json(path: Path) -> dict[str, object]:
    """Load a smartlist rule AST from a JSON file (raises OSError / JSONDecodeError)."""
    return json.loads(path.read_text())


def build_repo(path: Path | None = None) -> tuple[SmartlistsRepo, sqlite3.Connection]:
    conn = open_phase08_state_db(path)
    return SmartlistsRepo(conn, ensure_schema=False), conn


def smartlist_json(row: SmartlistRow) -> dict[str, object]:
    """Return the shared agent-readable smartlist snapshot with CAS revision."""
    return {
        "id": row.id,
        "name": row.name,
        "rule": row.rule,
        "rule_schema_version": row.rule_schema_version,
        "order_by": row.order_by,
        "referenced_fields": sorted(row.referenced_fields),
        "revision": smartlist_revision(row),
        "last_materialized_count": len(row.last_materialized_track_ids),
        "last_evaluated_at": (
            row.last_evaluated_at.isoformat()
            if row.last_evaluated_at is not None
            else None
        ),
        "created_at": row.created_at.isoformat(),
        "modified_at": row.modified_at.isoformat(),
    }


__all__ = ["build_repo", "read_rule_json", "smartlist_json"]
