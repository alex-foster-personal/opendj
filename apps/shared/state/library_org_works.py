"""Works and classical metadata writes, on the library organization journal.

Same choke point as :mod:`apps.shared.state.library_org`: every write here runs
in its ``_operation``, so stamps, sync and undo work the same way. Split out only
to keep that module small.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping
from typing import Any

from .library_org import (
    PRIMARY_KEYS,
    OrgError,
    WriteResult,
    _check_member_exists,
    _live,
    _operation,
    _read,
    _refuse_duplicate_work,
    _Unit,
    list_rows,
)
from .writer import StateWriter

#: The wrapped ``track_fields`` row holding per-recording classical metadata.
CLASSICAL_FIELD: str = "classical"
CLASSICAL_TEXT_KEYS: frozenset[str] = frozenset(
    {"composer", "catalogue", "movement", "soloist", "ensemble", "conductor", "label"}
)
CLASSICAL_YEAR_KEY: str = "recording_year"
MIN_RECORDING_YEAR: int = 1000
MAX_RECORDING_YEAR: int = 2999


def _member_key(row: Mapping[str, Any]) -> dict[str, Any]:
    return {name: row[name] for name in PRIMARY_KEYS["work_member"]}


def update_member(
    writer: StateWriter,
    session_id: str,
    work_id: str,
    member_kind: str,
    member_id: str,
    changes: Mapping[str, Any],
) -> WriteResult:
    """Change a member's role, or make this its primary work. Its place in the stack stays."""
    unknown = set(changes) - {"role", "is_primary"}
    if unknown:
        raise OrgError("VALIDATION", f"unknown fields: {sorted(unknown)}")
    cleared = sorted(name for name, value in changes.items() if value is None)
    if cleared:
        raise OrgError("VALIDATION", f"{cleared} cannot be null")
    key = {"work_id": work_id, "member_kind": member_kind, "member_id": member_id}
    with _operation(writer, session_id, "work.member") as unit:
        conn = writer._conn
        _live(conn, "work_member", key)
        fields = {k: int(v) if k == "is_primary" else v for k, v in changes.items()}
        if fields.get("is_primary"):
            for other in list_rows(
                conn, "work_member",
                {"member_kind": member_kind, "member_id": member_id, "is_primary": 1},
            ):
                if other["work_id"] != work_id:
                    unit.put("work_member", _member_key(other), {"is_primary": 0})
        try:
            row = unit.put("work_member", key, fields)
        except sqlite3.IntegrityError as exc:
            raise OrgError("VALIDATION", str(exc)) from exc
    return WriteResult(row, unit.undo_id)


def move_members(
    writer: StateWriter, session_id: str, work_id: str, member_ids: list[str]
) -> WriteResult:
    """Put a stack in this order. It must name every member, and only the stack changes."""
    with _operation(writer, session_id, "work.move") as unit:
        conn = writer._conn
        work = _live(conn, "work", {"id": work_id})
        members = {m["member_id"]: m for m in list_rows(conn, "work_member", {"work_id": work_id})}
        if sorted(member_ids) != sorted(members):
            raise OrgError("VALIDATION", "a stack order must name every member exactly once")
        for index, member_id in enumerate(member_ids):
            unit.put("work_member", _member_key(members[member_id]), {"sort_index": index})
        # A saved top track would pin itself first and undo the reorder on screen.
        if _read(conn, "work_pref", {"work_id": work_id}) is not None:
            unit.put("work_pref", {"work_id": work_id}, {"top_member_id": member_ids[0]})
    return WriteResult(work, unit.undo_id)


def _next_sort_index(conn: sqlite3.Connection, work_id: str) -> int:
    members = list_rows(conn, "work_member", {"work_id": work_id})
    return max((m["sort_index"] for m in members), default=-1) + 1


def _primary_works(conn: sqlite3.Connection, member_ids: list[str]) -> dict[str, str]:
    """Each track's primary work, for those that have one."""
    found: dict[str, str] = {}
    for member_id in member_ids:
        for row in list_rows(
            conn, "work_member",
            {"member_kind": "track", "member_id": member_id, "is_primary": 1},
        ):
            found[member_id] = row["work_id"]
    return found


def _stack_target(
    unit: _Unit,
    primaries: Mapping[str, str],
    work_id: str | None,
    title: str | None,
    kind: str,
) -> dict[str, Any]:
    """The one work a selection stacks into, creating it only when none exists."""
    conn = unit.writer._conn
    targets = set(primaries.values()) | ({work_id} if work_id else set())
    if len(targets) > 1:
        raise OrgError(
            "CONFLICT",
            "these tracks are filed under different works; merge them first",
            {"work_ids": sorted(targets)},
        )
    if targets:
        return _live(conn, "work", {"id": next(iter(targets))})
    name = (title or "").strip()
    if not name:
        raise OrgError("VALIDATION", "a new work needs a title")
    _refuse_duplicate_work(conn, None, name, None)
    return unit.put(
        "work", {"id": uuid.uuid4().hex},
        {"kind": kind, "title": name, "composer": None, "catalogue": None,
         "movement": None, "aliases": "[]", "musicbrainz_work_id": None, "iswc": None},
    )


def link_tracks(
    writer: StateWriter,
    session_id: str,
    member_ids: list[str],
    *,
    work_id: str | None = None,
    title: str | None = None,
    kind: str = "song",
    role: str = "remix",
) -> WriteResult:
    """Stack tracks: into ``work_id``, their shared work, or a new one called ``title``.

    Two different primary works are never resolved silently (E17): the refusal
    carries both ids so the caller can ask, then merge. Tracks already together
    write nothing (E16).
    """
    unique = list(dict.fromkeys(member_ids))
    if work_id is None and len(unique) < 2:
        raise OrgError("VALIDATION", "stacking needs at least two tracks")
    with _operation(writer, session_id, "work.link_many") as unit:
        conn = writer._conn
        for member_id in unique:
            _check_member_exists(conn, "track", member_id)
        primaries = _primary_works(conn, unique)
        work = _stack_target(unit, primaries, work_id, title, kind)
        next_index = _next_sort_index(conn, work["id"])
        for member_id in unique:
            if primaries.get(member_id) == work["id"]:
                continue
            try:
                unit.put(
                    "work_member",
                    {"work_id": work["id"], "member_kind": "track", "member_id": member_id},
                    {"role": role, "is_primary": 1, "sort_index": next_index},
                )
            except sqlite3.IntegrityError as exc:
                raise OrgError("VALIDATION", str(exc)) from exc
            next_index += 1
    return WriteResult(work, unit.undo_id)




def merge_works(
    writer: StateWriter, session_id: str, source_id: str, into_id: str
) -> WriteResult:
    """Move every member of ``source_id`` into ``into_id`` and retire the source (R-WORK-6)."""
    if source_id == into_id:
        raise OrgError("VALIDATION", "a work cannot be merged into itself")
    with _operation(writer, session_id, "work.merge") as unit:
        conn = writer._conn
        source = _live(conn, "work", {"id": source_id})
        target = _live(conn, "work", {"id": into_id})
        held = {
            (m["member_kind"], m["member_id"])
            for m in list_rows(conn, "work_member", {"work_id": into_id})
        }
        next_index = _next_sort_index(conn, into_id)
        members = sorted(
            list_rows(conn, "work_member", {"work_id": source_id}),
            key=lambda m: m["sort_index"],
        )
        for member in members:
            if (member["member_kind"], member["member_id"]) in held and member["is_primary"]:
                # Its primary work is retiring: the target becomes the primary, never none.
                unit.put(
                    "work_member",
                    {"work_id": into_id, "member_kind": member["member_kind"],
                     "member_id": member["member_id"]},
                    {"is_primary": 1},
                )
            elif (member["member_kind"], member["member_id"]) not in held:
                unit.put(
                    "work_member",
                    {"work_id": into_id, "member_kind": member["member_kind"],
                     "member_id": member["member_id"]},
                    {"role": member["role"], "is_primary": member["is_primary"],
                     "sort_index": next_index},
                )
                next_index += 1
            unit.tombstone("work_member", _member_key(member))
        aliases = [*json.loads(target["aliases"]), *json.loads(source["aliases"]), source["title"]]
        merged_aliases = [a for a in dict.fromkeys(aliases) if a != target["title"]]
        row = unit.put("work", {"id": into_id}, {"aliases": json.dumps(merged_aliases)})
        if _read(conn, "work_pref", {"work_id": source_id}) is not None:
            unit.tombstone("work_pref", {"work_id": source_id})
        unit.tombstone("work", {"id": source_id})
    return WriteResult(row, unit.undo_id)


def _checked_classical(changes: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(changes) - CLASSICAL_TEXT_KEYS - {CLASSICAL_YEAR_KEY}
    if unknown:
        raise OrgError("VALIDATION", f"unknown fields: {sorted(unknown)}")
    checked: dict[str, Any] = {}
    for name, value in changes.items():
        if value is None:
            checked[name] = None
        elif name == CLASSICAL_YEAR_KEY:
            valid_year = isinstance(value, int) and not isinstance(value, bool)
            if not valid_year or not MIN_RECORDING_YEAR <= value <= MAX_RECORDING_YEAR:
                raise OrgError("VALIDATION", f"{name} must be a year, got {value!r}")
            checked[name] = value
        elif isinstance(value, str):
            checked[name] = value.strip() or None
        else:
            raise OrgError("VALIDATION", f"{name} must be text, got {value!r}")
    return checked


def classical_values(conn: sqlite3.Connection, stable_id: str) -> dict[str, Any]:
    """A track's classical metadata, empty when it has none."""
    row = _read(conn, "track_fields", {"stable_id": stable_id, "field_name": CLASSICAL_FIELD})
    if row is None or row["deleted_at"] is not None:
        return {}
    return json.loads(row["value_json"])


def set_classical(
    writer: StateWriter, session_id: str, stable_id: str, changes: Mapping[str, Any]
) -> WriteResult:
    """Merge classical metadata into a track. Null or blank clears a value.

    It lives in the wrapped ``track_fields`` row ``classical``, so it syncs like
    any other annotation and never touches the track's work or its links (E26).
    """
    checked = _checked_classical(changes)
    key = {"stable_id": stable_id, "field_name": CLASSICAL_FIELD}
    with _operation(writer, session_id, "track.classical") as unit:
        conn = writer._conn
        _live(conn, "tracks", {"stable_id": stable_id})
        current = classical_values(conn, stable_id)
        merged = {k: v for k, v in {**current, **checked}.items() if v is not None}
        if merged != current:
            if merged:
                unit.put(
                    "track_fields", key,
                    {"value_json": json.dumps(merged, sort_keys=True, separators=(",", ":"),
                                              ensure_ascii=False),
                     "source": "webui", "confidence": 1.0, "modified_at": unit.now},
                )
            else:
                unit.tombstone("track_fields", key)
    return WriteResult({"stable_id": stable_id, **merged}, unit.undo_id)

