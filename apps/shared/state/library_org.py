"""Library organization writes: one choke point for stamps, rules and undo.

Every write here runs in one transaction that stamps the row, logs it to
``local_changelog`` and records a before/after snapshot in ``undo_journal``
(Library Organization PRD s6.9). Rules the schema deliberately leaves out so
sync cannot wedge (see :mod:`apps.shared.state.migrations_v26`) live here too.
"""
from __future__ import annotations

import json
import sqlite3
import unicodedata
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

from .writer import StateWriter

ErrorCode = Literal["NOT_FOUND", "CONFLICT", "INVARIANT", "VALIDATION"]

#: Primary key columns of every table this module writes.
PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "playlists": ("playlist_id",),
    "playlist_section": ("id",),
    "tag_type": ("id",),
    "tag_meta": ("name_norm",),
    "tag_synonym": ("term_norm", "tag_name_norm"),
    "work": ("id",),
    "work_member": ("work_id", "member_kind", "member_id"),
    "work_dismissal": ("a_member", "b_member"),
    "work_pref": ("work_id",),
}

#: Newest entries kept per session. R-UNDO-1 needs at least 50.
UNDO_DEPTH: int = 200
DEFAULT_SECTION_TITLE: str = "New section"


class OrgError(Exception):
    """A refused write, typed as R-API-2 asks."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class _Unit:
    """One journaled operation: the rows it changed, before and after."""

    writer: StateWriter
    now: str
    before: list[dict[str, Any]] = field(default_factory=list)
    after: list[dict[str, Any]] = field(default_factory=list)
    undo_id: str | None = None

    def put(self, table: str, key: Mapping[str, Any], fields: Mapping[str, Any]) -> dict[str, Any]:
        """Insert or update one live row. Identical fields write nothing."""
        conn = self.writer._conn
        current = _read(conn, table, key)
        if current is not None and current["deleted_at"] is None and all(
            current[name] == value for name, value in fields.items()
        ):
            return current
        self._store(table, key, {**fields, "deleted_at": None}, current)
        return self._record(table, key, current)

    def tombstone(self, table: str, key: Mapping[str, Any]) -> None:
        """Soft-delete one row. An already deleted row writes nothing."""
        current = _read(self.writer._conn, table, key)
        if current is None:
            raise OrgError("NOT_FOUND", f"{table} {dict(key)} does not exist")
        if current["deleted_at"] is not None:
            return
        self._store(table, key, {"deleted_at": self.now}, current)
        self._record(table, key, current)

    def _store(
        self,
        table: str,
        key: Mapping[str, Any],
        fields: Mapping[str, Any],
        current: dict[str, Any] | None,
    ) -> None:
        _store(self.writer, table, key, fields, current, self.now)

    def _record(
        self, table: str, key: Mapping[str, Any], current: dict[str, Any] | None
    ) -> dict[str, Any]:
        row = _read(self.writer._conn, table, key)
        assert row is not None, f"{table} {dict(key)} vanished inside its own transaction"
        self.before.append({"table": table, "key": dict(key), "row": current})
        self.after.append({"table": table, "key": dict(key), "row": row})
        return row


def _read(
    conn: sqlite3.Connection, table: str, key: Mapping[str, Any]
) -> dict[str, Any] | None:
    where = " AND ".join(f"{name} = ?" for name in key)
    cursor = conn.execute(f"SELECT * FROM {table} WHERE {where}", tuple(key.values()))
    names = [column[0] for column in cursor.description]
    row = cursor.fetchone()
    return None if row is None else dict(zip(names, tuple(row), strict=True))


def _store(
    writer: StateWriter,
    table: str,
    key: Mapping[str, Any],
    fields: Mapping[str, Any],
    current: dict[str, Any] | None,
    now: str,
) -> None:
    stamp = writer._stamp(table, tuple(key.values()), now)
    stamped = {
        **fields,
        "updated_at": stamp.updated_at,
        "origin_device_id": stamp.origin_device_id,
    }
    if current is None:
        values = {**key, **stamped, "created_at": now}
        columns = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        writer._conn.execute(
            f"INSERT INTO {table}({columns}) VALUES ({marks})", tuple(values.values())
        )
        return
    assignments = ", ".join(f"{name} = ?" for name in stamped)
    where = " AND ".join(f"{name} = ?" for name in key)
    writer._conn.execute(
        f"UPDATE {table} SET {assignments} WHERE {where}",
        (*stamped.values(), *key.values()),
    )


@contextmanager
def _operation(writer: StateWriter, session_id: str, op: str) -> Iterator[_Unit]:
    """Run one journaled operation in one transaction."""
    if not session_id.strip():
        raise OrgError("VALIDATION", "an undo session id is required")
    now = writer._now_iso()
    with writer._tx() as conn:
        unit = _Unit(writer=writer, now=now)
        yield unit
        if not unit.after:
            return
        unit.undo_id = uuid.uuid4().hex
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM undo_journal WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO undo_journal(id, session_id, ts, seq, op, before, after) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                unit.undo_id, session_id, now, seq, op,
                json.dumps(unit.before, sort_keys=True),
                json.dumps(unit.after, sort_keys=True),
            ),
        )
        conn.execute(
            "DELETE FROM undo_journal WHERE session_id = ? AND seq <= ?",
            (session_id, seq - UNDO_DEPTH),
        )


@dataclass(frozen=True)
class WriteResult:
    """The row a write left, and how to undo it. No change means no ``undo_id``."""

    row: dict[str, Any]
    undo_id: str | None


def normalize_tag_name(name: str) -> str:
    """The identity of a tag: NFKC, casefolded and trimmed (PRD s6.4)."""
    normalized = unicodedata.normalize("NFKC", name).casefold().strip()
    if not normalized:
        raise OrgError("VALIDATION", "a tag name cannot be blank")
    return normalized


def _live(conn: sqlite3.Connection, table: str, key: Mapping[str, Any]) -> dict[str, Any]:
    row = _read(conn, table, key)
    if row is None or row["deleted_at"] is not None:
        raise OrgError("NOT_FOUND", f"{table} {dict(key)} does not exist")
    return row


def list_rows(
    conn: sqlite3.Connection, table: str, where: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Live rows of ``table``, filtered by exact column values."""
    if table not in PRIMARY_KEYS:
        raise OrgError("VALIDATION", f"{table} is not a library organization table")
    filters = dict(where or {})
    clause = "".join(f" AND {name} = ?" for name in filters)
    cursor = conn.execute(
        f"SELECT * FROM {table} WHERE deleted_at IS NULL{clause}", tuple(filters.values())
    )
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, tuple(row), strict=True)) for row in cursor.fetchall()]


# ----- playlists --------------------------------------------------------------


def _check_parent(
    conn: sqlite3.Connection, playlist_id: str, parent_id: str, kind: str
) -> None:
    parent = _live(conn, "playlists", {"playlist_id": parent_id})
    if kind == "set":
        if parent["kind"] == "set":
            raise OrgError("INVARIANT", "a set cannot be nested under another set")
    elif parent["kind"] != "folder":
        raise OrgError("INVARIANT", f"only a folder can hold a {kind}")
    seen = {playlist_id}
    cursor: str | None = parent_id
    while cursor is not None:
        if cursor in seen:
            raise OrgError("INVARIANT", "that move would put a list inside itself")
        seen.add(cursor)
        row = _read(conn, "playlists", {"playlist_id": cursor})
        cursor = None if row is None else row["parent_id"]


def organize_playlist(
    writer: StateWriter, session_id: str, playlist_id: str, changes: Mapping[str, Any]
) -> WriteResult:
    """Change a playlist's ``parent_id``, ``kind``, ``stage``, ``set_status`` or ``sort_index``."""
    allowed = {"parent_id", "kind", "stage", "set_status", "sort_index"}
    unknown = set(changes) - allowed
    if unknown:
        raise OrgError("VALIDATION", f"unknown fields: {sorted(unknown)}")
    with _operation(writer, session_id, "playlist.organize") as unit:
        conn = writer._conn
        key = {"playlist_id": playlist_id}
        current = _live(conn, "playlists", key)
        merged = {**current, **changes}
        if merged["kind"] != "set" and merged["set_status"] is not None:
            raise OrgError("INVARIANT", "only a set has a set status")
        if merged["kind"] == "set" and merged["parent_id"] is None:
            raise OrgError("INVARIANT", "a set needs a parent list")
        if merged["parent_id"] is not None:
            _check_parent(conn, playlist_id, merged["parent_id"], merged["kind"])
        if merged["kind"] != "folder" and current["kind"] == "folder" and conn.execute(
            "SELECT 1 FROM playlists WHERE parent_id = ? AND deleted_at IS NULL LIMIT 1",
            (playlist_id,),
        ).fetchone():
            raise OrgError("INVARIANT", "a folder with sub-lists must stay a folder")
        try:
            row = unit.put("playlists", key, changes)
        except sqlite3.IntegrityError as exc:
            raise OrgError("VALIDATION", str(exc)) from exc
    return WriteResult(row, unit.undo_id)


# ----- sections ---------------------------------------------------------------


def add_section(
    writer: StateWriter,
    session_id: str,
    *,
    owner_kind: str,
    owner_id: str,
    before_position: int,
    title: str | None = None,
    color: str | None = None,
) -> WriteResult:
    """Add a section. A blank title becomes ``New section`` (D46)."""
    with _operation(writer, session_id, "section.add") as unit:
        if owner_kind == "playlist":
            _live(writer._conn, "playlists", {"playlist_id": owner_id})
        try:
            row = unit.put(
                "playlist_section",
                {"id": uuid.uuid4().hex},
                {
                    "owner_kind": owner_kind,
                    "owner_id": owner_id,
                    "before_position": before_position,
                    "title": (title or "").strip() or DEFAULT_SECTION_TITLE,
                    "color": color,
                },
            )
        except sqlite3.IntegrityError as exc:
            raise OrgError("VALIDATION", str(exc)) from exc
    return WriteResult(row, unit.undo_id)


def update_section(
    writer: StateWriter, session_id: str, section_id: str, changes: Mapping[str, Any]
) -> WriteResult:
    """Rename, recolour or move a section."""
    return _update(writer, session_id, "section.update", "playlist_section",
                   {"id": section_id}, changes, {"title", "color", "before_position"})


def delete_section(writer: StateWriter, session_id: str, section_id: str) -> str | None:
    """Tombstone a section; its tracks are untouched."""
    return _delete(writer, session_id, "section.delete", "playlist_section", {"id": section_id})


# ----- tags -------------------------------------------------------------------


def create_tag_type(
    writer: StateWriter, session_id: str, type_id: str, fields: Mapping[str, Any]
) -> WriteResult:
    """Create a tag type (or revive a deleted one with the same id)."""
    with _operation(writer, session_id, "tag_type.create") as unit:
        current = _read(writer._conn, "tag_type", {"id": type_id})
        if current is not None and current["deleted_at"] is None:
            raise OrgError("CONFLICT", f"tag type {type_id} already exists")
        row = unit.put("tag_type", {"id": type_id}, fields)
    return WriteResult(row, unit.undo_id)


def update_tag_type(
    writer: StateWriter, session_id: str, type_id: str, changes: Mapping[str, Any]
) -> WriteResult:
    """Edit a tag type's label or where it shows."""
    return _update(
        writer, session_id, "tag_type.update", "tag_type", {"id": type_id}, changes,
        {"label", "singular", "show_as_column", "show_in_tags", "column_width", "sort_index"},
    )


def put_tag_meta(
    writer: StateWriter,
    session_id: str,
    name: str,
    *,
    tag_type: str = "general",
    color: str | None = None,
    source_category: str | None = None,
) -> WriteResult:
    """Set a tag's type and colour. Tag VALUES stay in My Tag (PRD s6.4)."""
    with _operation(writer, session_id, "tag_meta.put") as unit:
        _live(writer._conn, "tag_type", {"id": tag_type})
        row = unit.put(
            "tag_meta",
            {"name_norm": normalize_tag_name(name)},
            {
                "display_name": name.strip(),
                "type": tag_type,
                "color": color,
                "source_category": source_category,
            },
        )
    return WriteResult(row, unit.undo_id)


def put_tag_synonym(writer: StateWriter, session_id: str, term: str, tag: str) -> WriteResult:
    """Teach the suggester that ``term`` means ``tag``."""
    with _operation(writer, session_id, "tag_synonym.put") as unit:
        row = unit.put(
            "tag_synonym",
            {"term_norm": normalize_tag_name(term), "tag_name_norm": normalize_tag_name(tag)},
            {},
        )
    return WriteResult(row, unit.undo_id)


# ----- works ------------------------------------------------------------------


def _checked_aliases(fields: Mapping[str, Any]) -> dict[str, Any]:
    checked = dict(fields)
    if "aliases" in checked:
        aliases = checked["aliases"]
        if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
            raise OrgError("VALIDATION", "aliases must be a list of strings")
        checked["aliases"] = json.dumps(aliases)
    return checked


def create_work(writer: StateWriter, session_id: str, fields: Mapping[str, Any]) -> WriteResult:
    """Create a work."""
    with _operation(writer, session_id, "work.create") as unit:
        try:
            row = unit.put("work", {"id": uuid.uuid4().hex}, _checked_aliases(fields))
        except sqlite3.IntegrityError as exc:
            raise OrgError("VALIDATION", str(exc)) from exc
    return WriteResult(row, unit.undo_id)


def update_work(
    writer: StateWriter, session_id: str, work_id: str, changes: Mapping[str, Any]
) -> WriteResult:
    """Edit a work."""
    return _update(
        writer, session_id, "work.update", "work", {"id": work_id}, _checked_aliases(changes),
        {"kind", "title", "composer", "catalogue", "movement", "aliases",
         "musicbrainz_work_id", "iswc"},
    )


def delete_work(writer: StateWriter, session_id: str, work_id: str) -> str | None:
    """Tombstone a work and its members together."""
    with _operation(writer, session_id, "work.delete") as unit:
        _live(writer._conn, "work", {"id": work_id})
        for member in list_rows(writer._conn, "work_member", {"work_id": work_id}):
            unit.tombstone("work_member", {name: member[name] for name in PRIMARY_KEYS["work_member"]})
        unit.tombstone("work", {"id": work_id})
    return unit.undo_id


def link_member(
    writer: StateWriter,
    session_id: str,
    work_id: str,
    *,
    member_kind: str,
    member_id: str,
    role: str,
    is_primary: bool = False,
    sort_index: int = 0,
) -> WriteResult:
    """Link a member. A new primary demotes the member's previous primary work."""
    with _operation(writer, session_id, "work.link") as unit:
        conn = writer._conn
        _live(conn, "work", {"id": work_id})
        if is_primary:
            for other in list_rows(
                conn, "work_member",
                {"member_kind": member_kind, "member_id": member_id, "is_primary": 1},
            ):
                if other["work_id"] != work_id:
                    unit.put(
                        "work_member",
                        {name: other[name] for name in PRIMARY_KEYS["work_member"]},
                        {"is_primary": 0},
                    )
        try:
            row = unit.put(
                "work_member",
                {"work_id": work_id, "member_kind": member_kind, "member_id": member_id},
                {"role": role, "is_primary": int(is_primary), "sort_index": sort_index},
            )
        except sqlite3.IntegrityError as exc:
            raise OrgError("VALIDATION", str(exc)) from exc
    return WriteResult(row, unit.undo_id)


def unlink_member(
    writer: StateWriter, session_id: str, work_id: str, member_kind: str, member_id: str
) -> str | None:
    """Remove a member from a work."""
    return _delete(
        writer, session_id, "work.unlink", "work_member",
        {"work_id": work_id, "member_kind": member_kind, "member_id": member_id},
    )


def dismiss_pair(
    writer: StateWriter, session_id: str, a_member: str, b_member: str, work_id: str | None = None
) -> WriteResult:
    """Remember "Not a version". The pair is unordered."""
    if a_member == b_member:
        raise OrgError("VALIDATION", "a member cannot be dismissed against itself")
    first, second = sorted((a_member, b_member))
    with _operation(writer, session_id, "work.dismiss") as unit:
        row = unit.put(
            "work_dismissal", {"a_member": first, "b_member": second}, {"work_id": work_id}
        )
    return WriteResult(row, unit.undo_id)


def set_work_pref(
    writer: StateWriter, session_id: str, work_id: str, top_member_id: str
) -> WriteResult:
    """Choose the member shown first. Display only."""
    with _operation(writer, session_id, "work.pref") as unit:
        _live(writer._conn, "work", {"id": work_id})
        row = unit.put("work_pref", {"work_id": work_id}, {"top_member_id": top_member_id})
    return WriteResult(row, unit.undo_id)


# ----- shared write shapes ----------------------------------------------------


def _update(
    writer: StateWriter,
    session_id: str,
    op: str,
    table: str,
    key: Mapping[str, Any],
    changes: Mapping[str, Any],
    allowed: set[str],
) -> WriteResult:
    unknown = set(changes) - allowed
    if unknown:
        raise OrgError("VALIDATION", f"unknown fields: {sorted(unknown)}")
    with _operation(writer, session_id, op) as unit:
        _live(writer._conn, table, key)
        try:
            row = unit.put(table, key, changes)
        except sqlite3.IntegrityError as exc:
            raise OrgError("VALIDATION", str(exc)) from exc
    return WriteResult(row, unit.undo_id)


def _delete(
    writer: StateWriter, session_id: str, op: str, table: str, key: Mapping[str, Any]
) -> str | None:
    with _operation(writer, session_id, op) as unit:
        unit.tombstone(table, key)
    return unit.undo_id


# ----- undo -------------------------------------------------------------------


def _content(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """A row without its stamps: an undo of a later entry re-stamps it unchanged."""
    if row is None:
        return None
    return {k: v for k, v in row.items() if k not in ("updated_at", "origin_device_id")}


@dataclass(frozen=True)
class UndoResult:
    """Which operation was reverted."""

    undo_id: str
    op: str


def undo_last(writer: StateWriter, session_id: str) -> UndoResult:
    """Revert the session's newest operation, or refuse and keep it (R-UNDO-2)."""
    now = writer._now_iso()
    with writer._tx() as conn:
        entry = conn.execute(
            "SELECT id, op, before, after FROM undo_journal "
            "WHERE session_id = ? AND undone_at IS NULL ORDER BY seq DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if entry is None:
            raise OrgError("NOT_FOUND", "nothing to undo")
        undo_id, op, before_json, after_json = tuple(entry)
        before, after = json.loads(before_json), json.loads(after_json)
        for change in after:
            if _content(_read(conn, change["table"], change["key"])) != _content(change["row"]):
                raise OrgError(
                    "CONFLICT",
                    f"cannot undo {op}: {change['table']} changed since, on this "
                    "machine or through sync",
                )
        for change in reversed(before):
            current = _read(conn, change["table"], change["key"])
            prior = change["row"]
            fields = (
                {"deleted_at": now}
                if prior is None
                else {
                    name: value
                    for name, value in prior.items()
                    if name not in change["key"]
                    and name not in ("created_at", "updated_at", "origin_device_id")
                }
            )
            _store(writer, change["table"], change["key"], fields, current, now)
        conn.execute("UPDATE undo_journal SET undone_at = ? WHERE id = ?", (now, undo_id))
    return UndoResult(undo_id, op)


