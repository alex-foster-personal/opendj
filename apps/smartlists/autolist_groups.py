"""Autolist bucket catalogs and selection-to-rule compiler (issue #2066).

Genre / rating / BPM groups compile to a smartlist rule AST for
:func:`apps.smartlists.evaluator.evaluate`. OR within a group, AND across
groups; empty selection returns ``None``.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

GROUP_IDS: tuple[str, ...] = ("genre", "rating", "bpm")
AutolistGroupId = Literal["genre", "rating", "bpm"]

GENRE_UNSPECIFIED_ID = "unspecified"
RATING_UNRATED_ID = "unrated"
BPM_UNSPECIFIED_ID = "unspecified"
BPM_LT60_ID = "lt60"
BPM_GE200_ID = "ge200"


@dataclass(frozen=True)
class Bucket:
    id: str
    label: str
    count: int | None = None


def bpm_bucket_key(bpm: float | None) -> str:
    if bpm is None:
        return BPM_UNSPECIFIED_ID
    if bpm < 60:
        return BPM_LT60_ID
    if bpm >= 200:
        return BPM_GE200_ID
    lo = int(bpm // 10) * 10
    if lo < 60:
        lo = 60
    return f"{lo}-{lo + 9}"


def rating_bucket_key(rating: float | None) -> str:
    if rating is None or rating == 0:
        return RATING_UNRATED_ID
    return str(int(rating))


def _genre_col_sql() -> str:
    return (
        "(SELECT json_extract(tf.value_json, '$') FROM track_fields tf "
        "WHERE tf.stable_id = tracks.stable_id AND tf.field_name = 'genre' LIMIT 1)"
    )


def _bpm_col_sql() -> str:
    return (
        "(SELECT json_extract(tf.value_json, '$') FROM track_fields tf "
        "WHERE tf.stable_id = tracks.stable_id AND tf.field_name = 'bpm' LIMIT 1)"
    )


def _rating_col_sql() -> str:
    return (
        "(SELECT json_extract(tf.value_json, '$') FROM track_fields tf "
        "WHERE tf.stable_id = tracks.stable_id AND tf.field_name = 'rating' LIMIT 1)"
    )


def static_rating_buckets() -> list[Bucket]:
    out: list[Bucket] = []
    for n in range(5, 0, -1):
        label = f"{n} star" if n == 1 else f"{n} stars"
        out.append(Bucket(id=str(n), label=label))
    out.append(Bucket(id=RATING_UNRATED_ID, label="Unrated"))
    return out


def static_bpm_buckets() -> list[Bucket]:
    out: list[Bucket] = [Bucket(id=BPM_LT60_ID, label="<60")]
    for lo in range(60, 200, 10):
        out.append(Bucket(id=f"{lo}-{lo + 9}", label=f"{lo}-{lo + 9}"))
    out.append(Bucket(id=BPM_GE200_ID, label=">=200"))
    out.append(Bucket(id=BPM_UNSPECIFIED_ID, label="Unspecified"))
    return out


def genre_buckets(conn: sqlite3.Connection) -> list[Bucket]:
    col = _genre_col_sql()
    rows = conn.execute(
        f"SELECT {col} AS genre, COUNT(*) AS cnt "
        "FROM tracks WHERE tracks.deleted_at IS NULL "
        f"GROUP BY {col} "
        "ORDER BY CASE WHEN genre IS NULL THEN 1 ELSE 0 END, genre"
    ).fetchall()
    out: list[Bucket] = []
    missing_count = 0
    for genre, cnt in rows:
        if genre is None:
            missing_count = int(cnt)
        else:
            out.append(Bucket(id=str(genre), label=str(genre), count=int(cnt)))
    if missing_count > 0 or not out:
        out.append(
            Bucket(id=GENRE_UNSPECIFIED_ID, label="Unspecified", count=missing_count)
        )
    return out


def _rating_rule(child_id: str) -> dict[str, Any]:
    if child_id == RATING_UNRATED_ID:
        return {
            "op": "or",
            "children": [
                {"field": "rating", "op": "=", "value": 0},
                {"field": "rating", "op": "missing", "value": None},
            ],
        }
    return {"field": "rating", "op": "=", "value": int(child_id)}


def _bpm_rule(child_id: str) -> dict[str, Any]:
    if child_id == BPM_UNSPECIFIED_ID:
        return {"field": "bpm", "op": "missing", "value": None}
    if child_id == BPM_LT60_ID:
        return {"field": "bpm", "op": "<", "value": 60}
    if child_id == BPM_GE200_ID:
        return {"field": "bpm", "op": ">=", "value": 200}
    if "-" in child_id:
        lo_s, hi_s = child_id.split("-", 1)
        lo, hi = int(lo_s), int(hi_s)
        return {
            "op": "and",
            "children": [
                {"field": "bpm", "op": ">=", "value": lo},
                {"field": "bpm", "op": "<", "value": hi + 1},
            ],
        }
    raise ValueError(f"unknown bpm bucket id {child_id!r}")


def _genre_rule(child_id: str) -> dict[str, Any]:
    if child_id == GENRE_UNSPECIFIED_ID:
        return {"field": "genre", "op": "missing", "value": None}
    return {"field": "genre", "op": "=", "value": child_id}


def _group_rule(group: str, child_ids: list[str]) -> dict[str, Any] | None:
    if not child_ids:
        return None
    rules: list[dict[str, Any]]
    if group == "genre":
        rules = [_genre_rule(cid) for cid in child_ids]
    elif group == "rating":
        rules = [_rating_rule(cid) for cid in child_ids]
    elif group == "bpm":
        rules = [_bpm_rule(cid) for cid in child_ids]
    else:
        raise ValueError(f"unknown autolist group {group!r}")
    if len(rules) == 1:
        return rules[0]
    return {"op": "or", "children": rules}


def selection_to_rule(selection: dict[str, list[str]]) -> dict[str, Any] | None:
    """Compile autolist tag selection to one rule AST, or None when empty."""
    parts: list[dict[str, Any]] = []
    for group in GROUP_IDS:
        child_ids = selection.get(group) or []
        rule = _group_rule(group, child_ids)
        if rule is not None:
            parts.append(rule)
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return {"op": "and", "children": parts}


def bucket_counts_for_group(
    conn: sqlite3.Connection, group: AutolistGroupId,
) -> dict[str, int]:
    if group == "genre":
        return {b.id: (b.count or 0) for b in genre_buckets(conn)}
    col = _rating_col_sql() if group == "rating" else _bpm_col_sql()
    key_fn = rating_bucket_key if group == "rating" else bpm_bucket_key
    rows = conn.execute(
        f"SELECT {col} AS val, COUNT(*) AS cnt "
        "FROM tracks WHERE tracks.deleted_at IS NULL "
        f"GROUP BY {col}"
    ).fetchall()
    counts: dict[str, int] = {}
    for val, cnt in rows:
        key = key_fn(val)
        counts[key] = counts.get(key, 0) + int(cnt)
    return counts


def buckets_with_counts(conn: sqlite3.Connection, group: AutolistGroupId) -> list[Bucket]:
    if group == "genre":
        return genre_buckets(conn)
    static = static_rating_buckets() if group == "rating" else static_bpm_buckets()
    counts = bucket_counts_for_group(conn, group)
    return [Bucket(id=b.id, label=b.label, count=counts.get(b.id, 0)) for b in static]


def compact_index_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Compact {stable_id, genre, rating, bpm} rows for the client worker."""
    genre_col = _genre_col_sql()
    rating_col = _rating_col_sql()
    bpm_col = _bpm_col_sql()
    rows = conn.execute(
        f"SELECT tracks.stable_id, {genre_col}, {rating_col}, {bpm_col} "
        "FROM tracks WHERE tracks.deleted_at IS NULL"
    ).fetchall()
    items: list[dict[str, Any]] = []
    for sid, genre, rating, bpm in rows:
        items.append({
            "stable_id": sid,
            "genre": genre,
            "rating": rating,
            "bpm": bpm,
        })
    return items


__all__ = [
    "Bucket",
    "GROUP_IDS",
    "bpm_bucket_key",
    "bucket_counts_for_group",
    "buckets_with_counts",
    "compact_index_rows",
    "genre_buckets",
    "rating_bucket_key",
    "selection_to_rule",
    "static_bpm_buckets",
    "static_rating_buckets",
]
