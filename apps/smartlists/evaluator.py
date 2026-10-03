"""Rule-AST -> SQL compiler + evaluator (SMART-02).

Compiles a validated smartlist rule into a parameterised SQL query over
the Phase 5 shared-state schema:

* ``tracks.stable_id`` is the primary identifier.
* Analytical fields (bpm, key, energy, rating, genre, custom_tags,
  color_tag, last_played) live in ``track_fields`` EAV keyed
  ``(stable_id, field_name)`` with ``value_json`` holding the typed
  value. The evaluator resolves them with a correlated subquery per
  predicate.
* ``added_date`` maps to ``tracks.created_at``.
* ``paired_with`` translates to a ``stable_id IN (SELECT ... FROM
  pairings ...)`` subquery (direction 'into' + 'either').

All value bindings flow through ``?`` parameter placeholders;
identifiers (field names, order-by columns) come from
:mod:`apps.shared.smartlists` enums only.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from apps.analysis.selection import PROJECTION_FIELDS, Selection, field_column_sql
from apps.shared.smartlists import (
    FIELD_TYPES,
    LOGICAL_OPS,
    SmartlistRuleError,
    validate_rule,
)


class EvaluatorError(ValueError):
    """Raised when a rule cannot be compiled (e.g. bad order_by)."""


_TRACKS_NATIVE: dict[str, str] = {
    "added_date": "tracks.created_at",
}

_ORDER_BY_COLUMNS: dict[str, str] = {
    "added_date asc":  "tracks.created_at ASC",
    "added_date desc": "tracks.created_at DESC",
    "bpm asc":         "_bpm ASC",
    "bpm desc":        "_bpm DESC",
    "rating asc":      "_rating ASC",
    "rating desc":     "_rating DESC",
    "energy asc":      "_energy ASC",
    "energy desc":     "_energy DESC",
    "random":          "RANDOM()",
}


def _sql_literal(s: str) -> str:
    if not s.replace("_", "").isalnum():
        raise EvaluatorError(f"refusing unsafe identifier {s!r}")
    return "'" + s + "'"


def _eav_scalar(field: str) -> str:
    return (
        "(SELECT json_extract(tf.value_json, '$') FROM track_fields tf "
        "WHERE tf.stable_id = tracks.stable_id AND tf.field_name = "
        f"{_sql_literal(field)} LIMIT 1)"
    )


def _resolve_relative_date(expr: dict) -> str:
    raw = expr["$relative"]
    if not raw or not isinstance(raw, str):
        raise SmartlistRuleError(f"invalid $relative value {raw!r}")
    unit = raw[-1]
    try:
        n = int(raw[:-1])
    except ValueError as exc:
        raise SmartlistRuleError(f"invalid $relative value {raw!r}") from exc
    delta_kwargs = {
        "d": {"days": n},
        "h": {"hours": n},
        "m": {"minutes": n},
        "w": {"weeks": n},
    }.get(unit)
    if delta_kwargs is None:
        raise SmartlistRuleError(
            f"$relative unit {unit!r} must be one of d/h/m/w"
        )
    return (datetime.now(UTC) + timedelta(**delta_kwargs)).isoformat()


def _prepare_date_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _resolve_relative_date(value)
    if isinstance(value, list):
        return [_prepare_date_value(v) for v in value]
    return value


def _paired_with_subquery(anchor_sql: str) -> str:
    """Tracks the anchor pairs into, whichever way round the edge is stored.

    ``(anchor, x, into|either)`` and ``(x, anchor, out_of|either)`` are the
    same pairing, matching ``rank_stage2``.
    """
    return (
        "SELECT to_stable_id FROM pairings "
        f"WHERE direction IN ('into','either') AND from_stable_id {anchor_sql} "
        "UNION SELECT from_stable_id FROM pairings "
        f"WHERE direction IN ('out_of','either') AND to_stable_id {anchor_sql}"
    )


def _compile_paired_with(op: str, value: Any) -> tuple[str, list[Any]]:
    if op == "=":
        return f"tracks.stable_id IN ({_paired_with_subquery('= ?')})", [value, value]
    if op == "!=":
        return (
            f"tracks.stable_id NOT IN ({_paired_with_subquery('= ?')})",
            [value, value],
        )
    if op == "in":
        if not value:
            return "0", []
        placeholders = ", ".join("?" for _ in value)
        return (
            f"tracks.stable_id IN ({_paired_with_subquery(f'IN ({placeholders})')})",
            [*value, *value],
        )
    raise SmartlistRuleError(
        f"paired_with: only = / != / in supported; got {op!r}"
    )


def _compile_scalar_op(
    field: str, op: str, col: str, value: Any, ftype: str,
) -> tuple[str, list[Any]]:
    if op in ("=", "!=", "<", "<=", ">", ">="):
        return f"{col} {op} ?", [value]
    if op == "between":
        lo, hi = value
        return f"{col} BETWEEN ? AND ?", [lo, hi]
    if op == "in":
        if not value:
            return "0", []
        placeholders = ", ".join("?" for _ in value)
        return f"{col} IN ({placeholders})", list(value)
    if op == "missing":
        return f"({col}) IS NULL", []
    if op == "contains":
        if ftype == "list":
            sql = (
                "EXISTS (SELECT 1 FROM track_fields tf2, "
                "json_each(tf2.value_json) je "
                "WHERE tf2.stable_id = tracks.stable_id "
                f"AND tf2.field_name = {_sql_literal(field)} "
                "AND je.value = ?)"
            )
            return sql, [value]
        return f"{col} LIKE ?", [f"%{value}%"]
    raise SmartlistRuleError(f"evaluator: unsupported op {op!r}")


def _compile_predicate(node: dict, selection: Selection) -> tuple[str, list[Any]]:
    field = node["field"]
    op = node["op"]
    value = node["value"]

    if field == "paired_with":
        return _compile_paired_with(op, value)

    # Lane-owned fields resolve through apps.analysis.selection, which is
    # also what the track read model calls: same module, same Selection,
    # same table-choice rule, so the two readers cannot disagree.
    if field in PROJECTION_FIELDS:
        col = field_column_sql(field, selection)
    else:
        col = _TRACKS_NATIVE.get(field) or _eav_scalar(field)
    ftype = FIELD_TYPES[field]

    if ftype == "date":
        value = _prepare_date_value(value)
    return _compile_scalar_op(field, op, col, value, ftype)


def compile_rule(rule: dict, *, selection: Selection) -> tuple[str, list[Any]]:
    """Compile a validated rule to ``(where_sql, params)`` under ``selection``.

    ``selection`` is REQUIRED and keyword-only on purpose. A default of
    all-rbx would compile a filter on a promoted lane against rekordbox
    data and return rows, which is a wrong answer that looks like a right
    one. The caller has a connection; it can resolve the real selection.
    """
    if "op" in rule and rule["op"] in LOGICAL_OPS:
        op = rule["op"]
        if op == "not":
            inner_sql, inner_params = compile_rule(
                rule["children"][0], selection=selection
            )
            return f"NOT ({inner_sql})", inner_params
        parts = [compile_rule(c, selection=selection) for c in rule["children"]]
        sql = "(" + f" {op.upper()} ".join(p[0] for p in parts) + ")"
        params = [p for part in parts for p in part[1]]
        return sql, params
    return _compile_predicate(rule, selection)


def _order_by_sql(order_by: str) -> str:
    expr = _ORDER_BY_COLUMNS.get(order_by)
    if expr is None:
        raise EvaluatorError(
            f"order_by {order_by!r} not in allowlist {sorted(_ORDER_BY_COLUMNS)}"
        )
    return expr


def _order_by_needs_join(order_by: str) -> str | None:
    mapping: dict[str, str] = {
        "bpm asc": "bpm", "bpm desc": "bpm",
        "rating asc": "rating", "rating desc": "rating",
        "energy asc": "energy", "energy desc": "energy",
    }
    return mapping.get(order_by)


def evaluate(
    rule: dict,
    conn: sqlite3.Connection,
    *,
    order_by: str = "added_date desc",
    limit: int | None = None,
    validate: bool = True,
    selection: Selection | None = None,
) -> list[str]:
    """Evaluate ``rule`` against ``conn`` and return ordered stable_ids.

    ``selection`` defaults to the live per-lane sources read off ``conn``,
    so a smartlist sees the same effective values the track list does.
    """
    if validate:
        validate_rule(rule)
    if selection is None:
        selection = Selection.resolve(conn)
    where_sql, params = compile_rule(rule, selection=selection)
    order_sql = _order_by_sql(order_by)
    eav_order_field = _order_by_needs_join(order_by)

    if eav_order_field is not None:
        # ORDER BY is a reader too: sorting a promoted lane by the
        # rekordbox column would order the list by a value no cell shows.
        if eav_order_field in PROJECTION_FIELDS:
            order_col = field_column_sql(eav_order_field, selection)
        else:
            order_col = (
                "(SELECT json_extract(tf_o.value_json, '$') FROM track_fields tf_o "
                "WHERE tf_o.stable_id = tracks.stable_id AND tf_o.field_name = "
                f"{_sql_literal(eav_order_field)} LIMIT 1)"
            )
        select = f"SELECT tracks.stable_id, {order_col} AS _{eav_order_field} "
    else:
        select = "SELECT tracks.stable_id "

    # ADR 08 point 5 / round 2 finding 4b: a tombstoned track must not
    # surface in a smartlist. Filtered in-query rather than by the caller
    # so every consumer of evaluate() gets the honest set for free.
    sql = (
        select + "FROM tracks WHERE tracks.deleted_at IS NULL AND ("
        + where_sql + ") ORDER BY " + order_sql
    )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"

    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        # Phase 5 tables may not exist yet on an empty state DB; report
        # rather than crash so the CLI surface stays helpful.
        raise EvaluatorError(
            f"evaluator could not query tracks: {exc}. "
            "Run the Phase 5 ingest first to populate tracks + track_fields."
        ) from exc
    return [r[0] for r in rows]


__all__ = ["EvaluatorError", "compile_rule", "evaluate"]
