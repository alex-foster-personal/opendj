"""``SmartlistsRepo`` -- CRUD over the ``smartlists`` table."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Iterator

from apps.shared.pairings.schema_sql import ensure_phase08_tables
from apps.shared.smartlists import (
    SmartlistRow,
    validate_rule,
)
from apps.shared.smartlists import (
    referenced_fields as compute_referenced_fields,
)

_ALLOWED_ORDER_BY: frozenset[str] = frozenset({
    "added_date desc", "added_date asc",
    "bpm asc", "bpm desc",
    "rating desc", "rating asc",
    "energy asc", "energy desc",
    "random",
})


class SmartlistsRepoError(ValueError):
    """Raised on invalid CRUD arguments (bad order_by, dup name, etc)."""


class SmartlistRevisionConflict(SmartlistsRepoError):
    """Raised when a rule replacement targets an obsolete row revision."""

    def __init__(self, current: SmartlistRow) -> None:
        self.current = current
        self.current_revision = smartlist_revision(current)
        super().__init__(
            f"smartlist {current.id!r} revision does not match current state"
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def smartlist_revision(row: SmartlistRow) -> str:
    """Return a deterministic opaque revision for the complete stored row."""
    payload = json.dumps(
        {
            "id": row.id,
            "name": row.name,
            "rule": row.rule,
            "rule_schema_version": row.rule_schema_version,
            "referenced_fields": sorted(row.referenced_fields),
            "order_by": row.order_by,
            "last_evaluated_at": (
                row.last_evaluated_at.isoformat()
                if row.last_evaluated_at is not None
                else None
            ),
            "last_materialized_track_ids": row.last_materialized_track_ids,
            "created_at": row.created_at.isoformat(),
            "modified_at": row.modified_at.isoformat(),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _row_to_model(row: tuple) -> SmartlistRow:
    rule_json = row[2]
    materialized = row[7]
    return SmartlistRow(
        id=row[0],
        name=row[1],
        rule=json.loads(rule_json),
        rule_schema_version=row[3],
        referenced_fields=frozenset(json.loads(row[4])),
        order_by=row[5],
        last_evaluated_at=_parse_iso(row[6]),
        last_materialized_track_ids=(
            json.loads(materialized) if materialized else []
        ),
        created_at=_parse_iso(row[8]) or datetime.fromtimestamp(0, timezone.utc),
        modified_at=_parse_iso(row[9]) or datetime.fromtimestamp(0, timezone.utc),
        _raw_rule_json=rule_json,
    )


_COLS: str = (
    "id, name, rule, rule_schema_version, referenced_fields, order_by, "
    "last_evaluated_at, last_materialized_track_ids, created_at, modified_at"
)


class SmartlistsRepo:
    """Thin CRUD over ``smartlists``."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        ensure_schema: bool = True,
    ) -> None:
        self.conn = conn
        if ensure_schema:
            ensure_phase08_tables(conn)

    def create(
        self,
        name: str,
        rule: dict,
        *,
        order_by: str = "added_date desc",
        rule_schema_version: int = 0,
    ) -> SmartlistRow:
        validate_rule(rule)
        if order_by not in _ALLOWED_ORDER_BY:
            raise SmartlistsRepoError(
                f"order_by {order_by!r} not in allowlist {sorted(_ALLOWED_ORDER_BY)}"
            )
        sid = uuid.uuid4().hex
        now = _now_iso()
        ref_fields = sorted(compute_referenced_fields(rule))
        rule_json = json.dumps(rule, sort_keys=True)
        try:
            self.conn.execute(
                f"INSERT INTO smartlists ({_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    sid, name, rule_json, rule_schema_version,
                    json.dumps(ref_fields), order_by,
                    None, None, now, now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise SmartlistsRepoError(
                f"smartlist with name {name!r} already exists"
            ) from exc
        return self.get_by_id(sid)  # type: ignore[return-value]

    def update_rule(
        self,
        smartlist_id: str,
        rule: dict,
        *,
        expected_revision: str,
        order_by: str | None = None,
    ) -> SmartlistRow:
        """CAS-replace one complete rule inside one explicit transaction."""
        validate_rule(rule)
        if order_by is not None and order_by not in _ALLOWED_ORDER_BY:
            raise SmartlistsRepoError(
                f"order_by {order_by!r} not in allowlist"
            )
        rule_json = json.dumps(rule, sort_keys=True)
        ref_fields = sorted(compute_referenced_fields(rule))
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            current = self.get_by_id(smartlist_id)
            if current is None:
                raise SmartlistsRepoError(
                    f"smartlist {smartlist_id!r} not found"
                )
            if expected_revision != smartlist_revision(current):
                raise SmartlistRevisionConflict(current)

            params: list[object] = [
                rule_json, json.dumps(ref_fields), _now_iso(),
            ]
            sql = (
                "UPDATE smartlists SET rule=?, referenced_fields=?, "
                "modified_at=?"
            )
            if order_by is not None:
                sql += ", order_by=?"
                params.append(order_by)
            sql += " WHERE id=?"
            params.append(smartlist_id)
            self.conn.execute(sql, params)

            updated = self.get_by_id(smartlist_id)
            if updated is None:
                raise SmartlistsRepoError(
                    f"smartlist {smartlist_id!r} disappeared during update"
                )
            self.conn.execute("COMMIT")
            return updated
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def mark_materialized(
        self,
        smartlist_id: str,
        track_ids: list[str],
    ) -> None:
        self.conn.execute(
            "UPDATE smartlists SET last_materialized_track_ids=?, "
            "last_evaluated_at=?, modified_at=? WHERE id=?",
            (
                json.dumps(track_ids),
                _now_iso(),
                _now_iso(),
                smartlist_id,
            ),
        )

    def delete(self, smartlist_id: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM smartlists WHERE id=?", (smartlist_id,),
        )
        return cur.rowcount > 0

    def delete_by_name(self, name: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM smartlists WHERE name=?", (name,),
        )
        return cur.rowcount > 0

    def get_by_id(self, smartlist_id: str) -> SmartlistRow | None:
        row = self.conn.execute(
            f"SELECT {_COLS} FROM smartlists WHERE id=?",
            (smartlist_id,),
        ).fetchone()
        return _row_to_model(row) if row is not None else None

    def get_by_name(self, name: str) -> SmartlistRow | None:
        row = self.conn.execute(
            f"SELECT {_COLS} FROM smartlists WHERE name=?",
            (name,),
        ).fetchone()
        return _row_to_model(row) if row is not None else None

    def list_all(self) -> Iterator[SmartlistRow]:
        for row in self.conn.execute(
            f"SELECT {_COLS} FROM smartlists ORDER BY name"
        ):
            yield _row_to_model(row)


__all__ = [
    "SmartlistRevisionConflict",
    "SmartlistsRepo",
    "SmartlistsRepoError",
    "smartlist_revision",
]
