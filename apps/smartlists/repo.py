"""``SmartlistsRepo`` -- CRUD over the ``smartlists`` table."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

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
    return datetime.now(UTC).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def _live_sql(conn: sqlite3.Connection) -> str:
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(smartlists)")
    }
    if "deleted_at" not in columns:
        return ""
    return " AND (deleted_at IS NULL OR deleted_at = '')"


def _tombstone_name(original: str, smartlist_id: str) -> str:
    return f"{original}::__deleted__{smartlist_id}"


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
        created_at=_parse_iso(row[8]) or datetime.fromtimestamp(0, UTC),
        modified_at=_parse_iso(row[9]) or datetime.fromtimestamp(0, UTC),
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

    def unused_name(self, base: str) -> str:
        """Return ``base`` or the first unused ``base N`` among live rows."""
        live = _live_sql(self.conn)
        if self.get_by_name(base) is None:
            return base
        n = 2
        while True:
            candidate = f"{base} {n}"
            row = self.conn.execute(
                f"SELECT 1 FROM smartlists WHERE name=?{live}",
                (candidate,),
            ).fetchone()
            if row is None:
                return candidate
            n += 1

    def create(
        self,
        name: str,
        rule: dict,
        *,
        order_by: str = "added_date desc",
        rule_schema_version: int = 0,
    ) -> SmartlistRow:
        validate_rule(rule)
        if not name.strip():
            raise SmartlistsRepoError("smartlist name must be non-empty")
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
        name: str | None = None,
    ) -> SmartlistRow:
        """CAS-replace one complete rule inside one explicit transaction."""
        validate_rule(rule)
        if order_by is not None and order_by not in _ALLOWED_ORDER_BY:
            raise SmartlistsRepoError(
                f"order_by {order_by!r} not in allowlist"
            )
        if name is not None and not name.strip():
            raise SmartlistsRepoError("smartlist name must be non-empty")
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
            if name is not None:
                sql += ", name=?"
                params.append(name)
            sql += f" WHERE id=?{_live_sql(self.conn)}"
            params.append(smartlist_id)
            try:
                self.conn.execute(sql, params)
            except sqlite3.IntegrityError as exc:
                if name is not None:
                    raise SmartlistsRepoError(
                        f"smartlist with name {name!r} already exists"
                    ) from exc
                raise

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
        live = _live_sql(self.conn)
        self.conn.execute(
            "UPDATE smartlists SET last_materialized_track_ids=?, "
            f"last_evaluated_at=?, modified_at=? WHERE id=?{live}",
            (
                json.dumps(track_ids),
                _now_iso(),
                _now_iso(),
                smartlist_id,
            ),
        )

    def delete(self, smartlist_id: str) -> bool:
        live = _live_sql(self.conn)
        row = self.conn.execute(
            f"SELECT name FROM smartlists WHERE id=?{live}",
            (smartlist_id,),
        ).fetchone()
        if row is None:
            return False
        original_name = row[0]
        now = _now_iso()
        cur = self.conn.execute(
            f"UPDATE smartlists SET deleted_at=?, name=?, modified_at=? "
            f"WHERE id=?{live}",
            (_now_iso(), _tombstone_name(original_name, smartlist_id), now, smartlist_id),
        )
        return cur.rowcount > 0

    def delete_by_name(self, name: str) -> bool:
        live = _live_sql(self.conn)
        row = self.conn.execute(
            f"SELECT id, name FROM smartlists WHERE name=?{live}",
            (name,),
        ).fetchone()
        if row is None:
            return False
        return self.delete(row[0])

    def duplicate(
        self,
        smartlist_id: str,
        *,
        name: str | None = None,
    ) -> SmartlistRow:
        source = self.get_by_id(smartlist_id)
        if source is None:
            raise SmartlistsRepoError(f"smartlist {smartlist_id!r} not found")
        copy_name = (
            name
            if name is not None
            else self.unused_name(f"{source.name} (copy)")
        )
        return self.create(
            copy_name,
            source.rule,
            order_by=source.order_by,
            rule_schema_version=source.rule_schema_version,
        )

    def get_by_id(self, smartlist_id: str) -> SmartlistRow | None:
        live = _live_sql(self.conn)
        row = self.conn.execute(
            f"SELECT {_COLS} FROM smartlists WHERE id=?{live}",
            (smartlist_id,),
        ).fetchone()
        return _row_to_model(row) if row is not None else None

    def get_by_name(self, name: str) -> SmartlistRow | None:
        live = _live_sql(self.conn)
        row = self.conn.execute(
            f"SELECT {_COLS} FROM smartlists WHERE name=?{live}",
            (name,),
        ).fetchone()
        return _row_to_model(row) if row is not None else None

    def list_all(self) -> Iterator[SmartlistRow]:
        live = _live_sql(self.conn)
        for row in self.conn.execute(
            f"SELECT {_COLS} FROM smartlists WHERE 1=1{live} ORDER BY name"
        ):
            yield _row_to_model(row)


__all__ = [
    "SmartlistRevisionConflict",
    "SmartlistsRepo",
    "SmartlistsRepoError",
    "smartlist_revision",
]
