"""CLOUDSYNC-07: collapse the same audio under two ``tracks`` primary keys.

Path-tier (``inferred``) ``stable_id`` is minted from a local path and mtime,
so it is never a merge key. Merge identity is ``content_hash`` or
tag-independent ``audio_hash`` first, then a
normalizable ISRC, then a fingerprint-tier ``stable_id`` (already a global
PK, so ordinary LWW covers it).

When an incoming ``tracks`` row shares a content identity with a stored row
under a different PK, the merge keeps one survivor (deterministic LWW, with
the smaller PK as the tie-break so both peers converge), remaps every child
that REFERENCES ``tracks(stable_id)`` onto that survivor, and drops the
loser. Conflicting identity signals quarantine the incoming row rather than
silently duplicating or silently collapsing.

Do not add ``tracks`` to :data:`apps.sync_hub.protocol_common.NATURAL_KEYS`.
That path hard-deletes UNIQUE-index collisions; children of ``tracks`` would
CASCADE or orphan. This module is the dedicated collapse-then-remap path.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from apps.shared.state.ids import normalise_isrc
from apps.shared.state.sync_stamp import LOCAL_CHANGELOG_TABLE
from apps.sync_hub import protocol
from apps.sync_hub.engine_common import (
    CHANGELOG_TABLES,
    HUB_CHANGELOG_TABLE,
    SyncApplyError,
)
from apps.sync_hub.engine_identity_queries import (
    StoredMatch,
    matches_by_hash,
    matches_by_isrc,
)
from apps.sync_hub.protocol import MEMBERSHIP_TABLE, SPEC_BY_TABLE, RowChange

log = logging.getLogger("apps.sync_hub.engine")

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

Kind = Literal["none", "conflict", "incoming_wins", "incoming_loses"]


class SyncIdentityPreflightError(RuntimeError):
    """First-sync refused: inferred-tier rows still lack content identity."""


@dataclass(frozen=True)
class IdentityDecision:
    """What content identity says about one incoming ``tracks`` row.

    ``kind == "none"`` means there is no different-PK stored row sharing a
    merge key, so ordinary PK LWW proceeds. ``stored_pks`` are the different
    PKs that matched; ``survivor_pk`` is set when the incoming row loses.
    """

    kind: Kind
    stored_pks: tuple[str, ...] = ()
    survivor_pk: str | None = None


def _ident(name: str) -> str:
    """Allowlist a SQL identifier before interpolating it into a PRAGMA."""
    if not _IDENT.match(name):
        raise SyncApplyError(f"refusing to interpolate {name!r} as a SQL identifier")
    return name


def _as_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _incoming_beats(change: RowChange, stored_key: tuple[str, str], stored_pk: str) -> bool:
    """Same LWW rule as natural-key duplicates: sort key, then smaller PK."""
    if change.sort_key != stored_key:
        return change.sort_key > stored_key
    return change.pk < (stored_pk,)


def _signals_conflict(incoming: Mapping[str, Any], stored: StoredMatch) -> bool:
    """True when two rows share one identity key but disagree on another.

    Same ``content_hash`` or ``audio_hash`` with two normalizable ISRCs, or the same
    normalizable ISRC with two ``content_hash`` values. Either is a silent
    dup or a silent collapse if we guessed.
    """
    incoming_hash = _as_text(incoming.get("content_hash"))
    stored_hash = _as_text(stored.content_hash)
    incoming_audio_hash = _as_text(incoming.get("audio_hash"))
    stored_audio_hash = _as_text(stored.audio_hash)
    incoming_isrc = normalise_isrc(_as_text(incoming.get("isrc")))
    stored_isrc = normalise_isrc(_as_text(stored.isrc))
    if (incoming_hash and stored_hash and incoming_hash == stored_hash) or (
        incoming_audio_hash and stored_audio_hash and incoming_audio_hash == stored_audio_hash
    ):
        return bool(incoming_isrc and stored_isrc and incoming_isrc != stored_isrc)
    if incoming_isrc and stored_isrc and incoming_isrc == stored_isrc:
        return bool(incoming_hash and stored_hash and incoming_hash != stored_hash)
    return False


def _find_matches(conn: sqlite3.Connection, change: RowChange) -> list[StoredMatch]:
    incoming_pk = change.pk[0]
    content_hash = _as_text(change.values.get("content_hash"))
    audio_hash = _as_text(change.values.get("audio_hash"))
    if content_hash or audio_hash:
        matches = matches_by_hash(conn, incoming_pk, content_hash, audio_hash)
        if matches:
            return matches
    isrc = normalise_isrc(_as_text(change.values.get("isrc")))
    if isrc:
        return matches_by_isrc(conn, incoming_pk, isrc, _as_text(change.values.get("isrc")))
    return []


def is_removed(values: Mapping[str, Any]) -> bool:
    """True when a ``tracks`` row is a tombstone, so it takes no part in a collapse.

    CLOUDSYNC-31: the stored side of a match already skips tombstones
    (``matches_by_hash`` and ``matches_by_isrc`` filter ``deleted_at IS
    NULL``); this is the incoming side of the same rule. A removed row and a
    live row with the same audio are a removal and a deliberate re-add, not
    two copies of one track: collapsing them either erased the tombstone
    (the removed row lost, was hard-deleted on its sender, and a rescan
    re-inserted it live) or erased the re-add (the tombstone won and the
    live row was dropped). The removed row is decided by its own primary
    key's lifecycle instead (:mod:`apps.sync_hub.track_lifecycle`).
    """
    return values.get(protocol.DELETED_AT) is not None


def resolve_track_identity(conn: sqlite3.Connection, change: RowChange) -> IdentityDecision:
    """Decide content identity for an incoming ``tracks`` row.

    Fingerprint-tier PKs are already global; this function only fires for a
    different stored PK sharing ``content_hash`` or a normalizable ISRC.
    Path-tier ``stable_id`` is ignored on purpose. Rows offered as
    ``hash_pending`` skip merge-key lookup until a hash arrives (ADR-0068).
    """
    if change.table != "tracks":
        return IdentityDecision(kind="none")
    if change.hash_pending:
        return IdentityDecision(kind="none")
    if is_removed(change.values):
        return IdentityDecision(kind="none")
    matches = _find_matches(conn, change)
    if not matches:
        return IdentityDecision(kind="none")
    stored_pks = tuple(match.pk for match in matches)
    if any(_signals_conflict(change.values, match) for match in matches):
        return IdentityDecision(kind="conflict", stored_pks=stored_pks)
    if all(_incoming_beats(change, match.sort_key, match.pk) for match in matches):
        return IdentityDecision(kind="incoming_wins", stored_pks=stored_pks)
    champion = max(
        matches,
        key=lambda match: (match.sort_key, _invert_pk(match.pk)),
    )
    return IdentityDecision(
        kind="incoming_loses",
        stored_pks=stored_pks,
        survivor_pk=champion.pk,
    )


def _invert_pk(pk: str) -> str:
    """Sort key so ``max`` prefers the smaller PK on a stamp tie.

    LWW on equality keeps the smaller PK (both peers must converge on the
    same survivor). ``max`` over ``(sort_key, invert)`` does that without
    a custom comparator.
    """
    return "".join(chr(255 - ord(ch)) for ch in pk)


def _follow_remap(remap: Mapping[str, str], pk: str) -> str:
    """Walk ``pk -> survivor`` until it stops, so a chain of collapses lands."""
    seen: set[str] = set()
    current = pk
    while current in remap and current not in seen:
        seen.add(current)
        current = remap[current]
    return current


def rewrite_incoming_change(change: RowChange, remap: Mapping[str, str]) -> RowChange:
    """Rewrite loser PKs in a later row of the same batch onto the survivor.

    Incoming children of a losing ``tracks`` row still name the loser's
    ``stable_id``. Applying them as-is is a FOREIGN KEY 409. Rewriting them
    onto the survivor is what lets both machines' locations land. The map
    may include remaps persisted from earlier HTTP batches.
    """
    if not remap:
        return change
    values = dict(change.values)
    pk = list(change.pk)
    rewritten = False
    old = _as_text(values.get("stable_id"))
    if old is not None and old in remap:
        survivor = _follow_remap(remap, old)
        values["stable_id"] = survivor
        rewritten = True
        spec = SPEC_BY_TABLE.get(change.table)
        if spec is not None:
            for index, column in enumerate(spec.pk):
                if column == "stable_id":
                    pk[index] = survivor
    members = change.members
    if members is not None:
        new_members: list[dict[str, Any]] = []
        for member in members:
            item = dict(member)
            member_id = _as_text(item.get("stable_id"))
            if member_id is not None and member_id in remap:
                item["stable_id"] = _follow_remap(remap, member_id)
                rewritten = True
            new_members.append(item)
        members = tuple(new_members)
    if not rewritten:
        return change
    return RowChange(
        table=change.table,
        pk=tuple(pk),
        values=values,
        members=members,
    )


def names_held_parent(change: RowChange, held: set[str]) -> bool:
    """True when this row (or its membership bundle) names a held track PK."""
    if not held:
        return False
    sid = _as_text(change.values.get("stable_id"))
    if sid is not None and sid in held:
        return True
    if change.members:
        return any((_as_text(member.get("stable_id")) or "") in held for member in change.members)
    return False


def _child_tables(conn: sqlite3.Connection) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Tables whose FK ``from`` column is ``stable_id`` referencing ``tracks``."""
    found: list[tuple[str, tuple[str, ...]]] = []
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY 1"):
        table = str(name)
        if not _IDENT.match(table) or table == "tracks":
            continue
        fks = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
        if not any(fk[2] == "tracks" and fk[3] == "stable_id" for fk in fks):
            continue
        pk = tuple(str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})") if row[5])
        found.append((table, pk))
    return tuple(found)


def _table_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(str(row[1]) for row in conn.execute(f"PRAGMA table_info({_ident(table)})"))


def _rekey_child_changelog(
    conn: sqlite3.Connection,
    table: str,
    moves: Sequence[tuple[Sequence[Any], Sequence[Any] | None]],
) -> None:
    """Carry each moved row's changelog entries to its new key, or drop them.

    ``moves`` pairs a loser row's old key with the key it was written under
    on the survivor, or ``None`` where the survivor already held that key and
    the loser's row was discarded. A moved row keeps its entries, seq
    included, so a write still above the push fence is offered under the key
    it now lives at; dropping them left a row the offer never selects and the
    digest still counts (CLOUDSYNC-32: an edit on a settled loser raised the
    ADR 04 c6 alarm on every sync).
    """
    if table not in SPEC_BY_TABLE and table != MEMBERSHIP_TABLE:
        return
    changelogs = [name for name in (HUB_CHANGELOG_TABLE, LOCAL_CHANGELOG_TABLE) if name in CHANGELOG_TABLES]
    for old_pk, new_pk in moves:
        row_pk = protocol.encode_row_pk(tuple(str(part) for part in old_pk))
        for changelog in changelogs:
            _rekey_one(conn, changelog, table, row_pk, new_pk)


def _rekey_one(
    conn: sqlite3.Connection, changelog: str, table: str, row_pk: str, new_pk: Sequence[Any] | None
) -> None:
    if new_pk is None:
        conn.execute(f"DELETE FROM {changelog} WHERE table_name = ? AND row_pk = ?", (table, row_pk))
        return
    conn.execute(
        f"UPDATE {changelog} SET row_pk = ? WHERE table_name = ? AND row_pk = ?",
        (protocol.encode_row_pk(tuple(str(part) for part in new_pk)), table, row_pk),
    )


def _key_held(conn: sqlite3.Connection, table: str, pk: Sequence[str], key: Sequence[Any]) -> bool:
    where_pk = " AND ".join(f"{_ident(column)} IS ?" for column in pk)
    return conn.execute(f"SELECT 1 FROM {_ident(table)} WHERE {where_pk}", tuple(key)).fetchone() is not None


def _survivor_keys(
    conn: sqlite3.Connection, table: str, pk: Sequence[str], loser: str, survivor: str
) -> list[tuple[tuple[Any, ...], tuple[Any, ...]]]:
    """Each loser row's key paired with the same key under ``survivor``."""
    rows = conn.execute(
        f"SELECT {', '.join(_ident(column) for column in pk)} FROM {_ident(table)} WHERE stable_id = ?",
        (loser,),
    ).fetchall()
    return [
        (tuple(old), tuple(survivor if column == "stable_id" else value for column, value in zip(pk, old, strict=True)))
        for old in rows
    ]


def _remap_pk_includes_stable_id(
    conn: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
    pk: Sequence[str],
    loser: str,
    survivor: str,
) -> None:
    """Copy the loser's rows onto the survivor's key, keep the survivor's on a clash.

    A key the survivor already held is decided before the copy, and a key
    the copy did not write (another UNIQUE index refused it) is checked
    after, so only a row that really moved keeps its changelog entries.
    """
    planned = _survivor_keys(conn, table, pk, loser, survivor)
    free = [(old, new) for old, new in planned if not _key_held(conn, table, pk, new)]
    select_list = ", ".join("?" if column == "stable_id" else _ident(column) for column in columns)
    conn.execute(
        f"INSERT OR IGNORE INTO {_ident(table)} "
        f"({', '.join(_ident(column) for column in columns)}) "
        f"SELECT {select_list} FROM {_ident(table)} WHERE stable_id = ?",
        (survivor, loser),
    )
    moved = {old: new for old, new in free if _key_held(conn, table, pk, new)}
    conn.execute(f"DELETE FROM {_ident(table)} WHERE stable_id = ?", (loser,))
    _rekey_child_changelog(conn, table, [(old, moved.get(old)) for old, _new in planned])


def _remap_locations(conn: sqlite3.Connection, loser: str, survivor: str) -> None:
    rows = conn.execute(
        """
        SELECT location_id, machine_id, kind, file_path, remote_url
        FROM track_locations WHERE stable_id = ?
        """,
        (loser,),
    ).fetchall()
    for location_id, machine_id, kind, file_path, remote_url in rows:
        conflict = conn.execute(
            """
            SELECT location_id FROM track_locations
            WHERE stable_id = ?
              AND (machine_id IS ? OR (machine_id IS NULL AND ? IS NULL))
              AND kind = ?
              AND (
                    (file_path IS NOT NULL AND file_path = ?)
                 OR (remote_url IS NOT NULL AND remote_url = ?)
              )
              AND location_id != ?
            """,
            (
                survivor,
                machine_id,
                machine_id,
                kind,
                file_path,
                remote_url,
                location_id,
            ),
        ).fetchone()
        if conflict is not None:
            conn.execute(
                "DELETE FROM track_locations WHERE location_id = ?",
                (location_id,),
            )
            continue
        conn.execute(
            "UPDATE track_locations SET stable_id = ? WHERE location_id = ?",
            (survivor, location_id),
        )


def _remap_memberships(conn: sqlite3.Connection, loser: str, survivor: str) -> None:
    playlist_ids = [
        str(row[0])
        for row in conn.execute(
            "SELECT DISTINCT playlist_id FROM playlist_memberships WHERE stable_id = ?",
            (loser,),
        )
    ]
    for playlist_id in playlist_ids:
        already = conn.execute(
            "SELECT 1 FROM playlist_memberships WHERE playlist_id = ? AND stable_id = ? LIMIT 1",
            (playlist_id, survivor),
        ).fetchone()
        if already is not None:
            conn.execute(
                "DELETE FROM playlist_memberships WHERE playlist_id = ? AND stable_id = ?",
                (playlist_id, loser),
            )
            continue
        conn.execute(
            "UPDATE playlist_memberships SET stable_id = ? WHERE playlist_id = ? AND stable_id = ?",
            (survivor, playlist_id, loser),
        )


def _remap_update(conn: sqlite3.Connection, table: str, loser: str, survivor: str) -> None:
    conn.execute("SAVEPOINT remap_sid")
    try:
        conn.execute(
            f"UPDATE {_ident(table)} SET stable_id = ? WHERE stable_id = ?",
            (survivor, loser),
        )
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK TO remap_sid")
        conn.execute(f"DELETE FROM {_ident(table)} WHERE stable_id = ?", (loser,))
    conn.execute("RELEASE remap_sid")


#: Bound on SQL parameters per ``IN (...)`` probe in :func:`losers_with_children`.
_CHILD_PROBE_CHUNK: int = 500


def losers_with_children(conn: sqlite3.Connection, losers: Iterable[str]) -> set[str]:
    """The ``losers`` that still own a row in some ``tracks(stable_id)`` child table.

    :func:`remap_track_children` only reads and writes rows WHERE
    ``stable_id = loser`` in exactly these tables, so for every other loser it
    is a no-op. A read, safe outside any transaction.
    """
    wanted = list(dict.fromkeys(losers))
    found: set[str] = set()
    for table, _pk in _child_tables(conn):
        for start in range(0, len(wanted), _CHILD_PROBE_CHUNK):
            chunk = wanted[start : start + _CHILD_PROBE_CHUNK]
            placeholders = ", ".join("?" for _ in chunk)
            found.update(
                str(row[0])
                for row in conn.execute(
                    f"SELECT DISTINCT stable_id FROM {_ident(table)} WHERE stable_id IN ({placeholders})",
                    chunk,
                )
            )
    return found


def remap_track_children(conn: sqlite3.Connection, loser: str, survivor: str) -> None:
    """Point every ``tracks(stable_id)`` child at ``survivor``, then the loser
    row can be dropped without CASCADE-deleting those children."""
    if loser == survivor:
        return
    for table, pk in _child_tables(conn):
        if table == "track_locations":
            _remap_locations(conn, loser, survivor)
            continue
        if table == MEMBERSHIP_TABLE:
            _remap_memberships(conn, loser, survivor)
            continue
        columns = _table_columns(conn, table)
        if "stable_id" in pk:
            _remap_pk_includes_stable_id(conn, table, columns, pk, loser, survivor)
            continue
        _remap_update(conn, table, loser, survivor)


def unsyncable_inferred_pks(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Live inferred-tier ``tracks`` rows with no ``content_hash`` and no ISRC."""
    rows = conn.execute(
        """
        SELECT stable_id, isrc FROM tracks
        WHERE deleted_at IS NULL
          AND stable_id_tier = 'inferred'
          AND (content_hash IS NULL OR content_hash = '')
          AND (audio_hash IS NULL OR audio_hash = '')
        """
    ).fetchall()
    return tuple(str(pk) for pk, isrc in rows if normalise_isrc(_as_text(isrc)) is None)


def hub_library_size(conn: sqlite3.Connection) -> int:
    """How many live ``tracks`` rows this database holds.

    A COUNT, deliberately, and not the set of ``origin_device_id`` values that
    authored them. Attribution looked like the better signal and is not: a
    library migrated from an older schema carries ``origin_device_id IS NULL``
    on every row, so the authoring machine is simply not recorded. Measured on
    the author's own 9194-track library after its first seed, Sat 12 Sep 2026 --
    every hub row came back unattributed. An origin-based check would then read
    its OWN seeded rows as a foreign library and refuse that machine's second
    sync. A count cannot be wrong that way: it answers the only question
    a caller asks of it, which is whether a library is already here.
    """
    return int(conn.execute("SELECT COUNT(*) FROM tracks WHERE deleted_at IS NULL").fetchone()[0])


def log_hash_conflict(
    stable_id: str,
    incoming_hash: str,
    stored_hash: str,
    incoming_stamp: tuple[str, str],
    stored_stamp: tuple[str, str],
) -> None:
    """Log when two pushes disagree on ``content_hash`` for one ``stable_id``."""
    log.warning(
        "tracks %s: conflicting content_hash values incoming=%r stored=%r; "
        "incoming stamp=%s origin=%s stored stamp=%s origin=%s; "
        "last-writer-wins keeps one row",
        stable_id,
        incoming_hash,
        stored_hash,
        incoming_stamp[0],
        incoming_stamp[1],
        stored_stamp[0],
        stored_stamp[1],
    )


def assert_identity_ready(conn: sqlite3.Connection) -> None:
    """Refuse to start a sync while inferred-tier rows still lack identity.

    Superseded for hash_pending rows (ADR-0068): they travel to the hub and
    backfill later. This guard now applies only when a caller explicitly
    opts out of hash_pending (legacy tests and preflight helpers).
    """
    unsyncable = unsyncable_inferred_pks(conn)
    if not unsyncable:
        return
    raise SyncIdentityPreflightError(
        f"{len(unsyncable)} live inferred-tier tracks row(s) have no "
        f"content_hash and no normalizable ISRC; merging them into a library "
        f"another machine already seeded would duplicate overlapping "
        f"recordings. Rows whose audio is still on this machine: backfill "
        f"with `python -m apps.shared.state.backfill_content_hash --live`. "
        f"Rows whose audio is missing CANNOT be hashed and the backfill will "
        f"not move them -- relink those first (`/fix-links`), because a hash "
        f"needs a file to read."
    )


__all__ = [
    "IdentityDecision",
    "SyncIdentityPreflightError",
    "_follow_remap",
    "assert_identity_ready",
    "hub_library_size",
    "is_removed",
    "log_hash_conflict",
    "losers_with_children",
    "names_held_parent",
    "remap_track_children",
    "resolve_track_identity",
    "rewrite_incoming_change",
    "unsyncable_inferred_pks",
]
