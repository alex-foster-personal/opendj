"""Text-field smartlist predicates: title, artist, album, comments (issue #10022).

rekordbox intelligent-playlist text conditions, mimicked: every op compares
case-insensitively (SQLite ``lower``, ASCII folding) after trimming
surrounding whitespace on both the rule value and the stored value.

* ``title`` and ``album`` are native ``tracks`` columns.
* ``comments`` is the ``track_fields`` EAV row of that name.
* ``artist`` matches ANY credited artist in the ``tracks.artists_json`` list.

``!=`` keeps tracks whose value is absent (rekordbox "is not" does), and
``missing`` treats NULL, empty and whitespace-only alike.
"""

from __future__ import annotations

from typing import Any

from apps.shared.smartlists import SmartlistRuleError

TEXT_FIELDS: tuple[str, ...] = ("title", "artist", "album", "comments")

_COMMENTS_SQL = (
    "(SELECT json_extract(tf.value_json, '$') FROM track_fields tf "
    "WHERE tf.stable_id = tracks.stable_id AND tf.field_name = 'comments' LIMIT 1)"
)
_SCALAR_COLUMNS: dict[str, str] = {
    "title": "tracks.title",
    "album": "tracks.album",
    "comments": _COMMENTS_SQL,
}
_ARTIST_ELEMENT = "je.value"
_ARTIST_PRESENT = "NULLIF(trim(je.value), '') IS NOT NULL"


# ----------------------------------------------------------------------------
# value normalisation
# ----------------------------------------------------------------------------


def _normalise(value: str) -> str:
    return value.strip().lower()


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ----------------------------------------------------------------------------
# condition builders
# ----------------------------------------------------------------------------


def _positive_condition(expr: str, op: str, value: Any) -> tuple[str, list[Any]]:
    """SQL for ``=``, ``contains`` or ``in`` on one text expression."""
    folded = f"lower(trim({expr}))"
    if op == "=":
        return f"{folded} = ?", [_normalise(value)]
    if op == "contains":
        return f"{folded} LIKE ? ESCAPE '\\'", [f"%{_escape_like(_normalise(value))}%"]
    if op == "in":
        if not value:
            return "0", []
        placeholders = ", ".join("?" for _ in value)
        return f"{folded} IN ({placeholders})", [_normalise(v) for v in value]
    raise SmartlistRuleError(f"text field: unsupported op {op!r}")


def _any_artist(condition: str) -> str:
    return f"EXISTS (SELECT 1 FROM json_each(tracks.artists_json) je WHERE {condition})"


def _compile_artist(op: str, value: Any) -> tuple[str, list[Any]]:
    if op == "missing":
        return f"NOT {_any_artist(_ARTIST_PRESENT)}", []
    if op == "!=":
        condition, params = _positive_condition(_ARTIST_ELEMENT, "=", value)
        return f"NOT {_any_artist(condition)}", params
    condition, params = _positive_condition(_ARTIST_ELEMENT, op, value)
    return _any_artist(condition), params


def _compile_scalar(column: str, op: str, value: Any) -> tuple[str, list[Any]]:
    if op == "missing":
        return f"NULLIF(trim({column}), '') IS NULL", []
    if op == "!=":
        condition, params = _positive_condition(column, "=", value)
        return f"({column} IS NULL OR NOT ({condition}))", params
    return _positive_condition(column, op, value)


def compile_text_predicate(field: str, op: str, value: Any) -> tuple[str, list[Any]]:
    """Compile a validated text-field predicate to ``(where_sql, params)``."""
    if field == "artist":
        return _compile_artist(op, value)
    if field in _SCALAR_COLUMNS:
        return _compile_scalar(_SCALAR_COLUMNS[field], op, value)
    raise SmartlistRuleError(f"{field!r} is not a text field; expected one of {TEXT_FIELDS}")


__all__ = ["TEXT_FIELDS", "compile_text_predicate"]
