"""The sync-set walk: :class:`HeldKeys` and the per-row verdict every walk shares.

Split out of :mod:`apps.sync_hub.sync_set` (issue #9725, parent #1390) and
re-exported there; that module's docstring is the rationale for the FK
cascade and for why :class:`HeldKeys` reads parents the walk never visited.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from apps.sync_hub.protocol_common import (
    DELETED_AT,
    MEMBERSHIP_SPEC,
    MEMBERSHIP_TABLE,
    MODIFIED_AT,
    SPEC_BY_TABLE,
    TRACK_FIELDS_TABLE,
    UPDATED_AT,
    SyncProtocolError,
    TableSpec,
    describe_faults,
    stored_stamp_faults,
    table_columns,
)
from apps.sync_hub.sync_set_identity import (
    _track_identity_reason,
    identity_duplicate_remap,
    library_has_identity_keys,
)
from apps.sync_hub.sync_set_reasons import (
    _MISSING_ROW,
    FK_ORDER,
    IDENTITY_DUP_REASON,
    IDENTITY_HOLD_REASON,
    IDENTITY_SETTLED_REASON,
    MEMBER_HELD_PREFIX,
    PARENT_HELD_SUFFIX,
    PARENT_KEYS,
    PARENT_MISSING_SUFFIX,
    SETTLED_PARENT_SUFFIX,
    is_settled,
)


class HeldKeys:
    """Which rows are in the sync set, answered for a whole walk.

    Mutable and single-pass by design: one instance is threaded through one
    walk, filled as rows are decided. Sharing it between two walks would
    carry a stale answer into a later one.

    It caches BOTH verdicts, held and free, and reads ``conn`` for any parent
    the walk has not decided yet (see the module docstring). Caching the free
    ones is what keeps a full walk at zero extra queries: every parent is
    already recorded by the time its dependants are judged, so the read path
    only ever fires for the fenced and chunked walks that genuinely cannot
    see their parents.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.keys: dict[str, set[str]] = {}
        self._free: dict[str, set[str]] = {}
        self._resolving: set[tuple[str, str]] = set()
        self._settled: set[tuple[str, str]] = set()
        self._missing: set[tuple[str, str]] = set()
        self._identity_elections = library_has_identity_keys(conn)
        from apps.sync_hub.identity_verdicts import IdentityLoserVerdicts

        self._identity = IdentityLoserVerdicts(conn)

    @property
    def identity_elections_enabled(self) -> bool:
        """False when no live track carries a mergeable content-identity key."""
        return self._identity_elections

    def is_identity_loser(self, stable_id: str) -> bool:
        """True when this ``tracks`` row loses an identity election to another row.

        Decided per row, from the rows sharing its identity keys, rather than
        by reading the whole library when the walk starts: a hub pull page is
        one walk, and a first sync of 10,000 tracks read the library once per
        page (LIBM-120 L6).
        """
        if not self._identity_elections:
            return False
        return self._identity.is_loser(stable_id)

    def loser_reason(self, stable_id: str) -> str | None:
        """Why this ``tracks`` row is out as an identity loser, or None when it is not one.

        A loser the hub has already answered for is SETTLED (CLOUDSYNC-32) and
        recorded so, which lets its children follow it in :func:`row_reason`.
        """
        if not self.is_identity_loser(stable_id):
            return None
        if not self._identity.is_hub_answered(stable_id):
            return IDENTITY_DUP_REASON
        self._settled.add(("tracks", stable_id))
        return IDENTITY_SETTLED_REASON

    def is_missing(self, table: str, key: str) -> bool:
        """True when ``table``/``key`` was held in this walk because no row stores it."""
        return (table, key) in self._missing

    def is_settled(self, table: str, key: str) -> bool:
        """True when ``table``/``key`` was held as a settled duplicate in this walk."""
        return (table, key) in self._settled

    def hold(self, table: str, pk: Sequence[str]) -> None:
        """Record ``pk``'s first column as held under ``table``.

        Parents named in :data:`PARENT_KEYS` are looked up again when judging
        children (for example ``tracks`` for ``track_edit`` and
        ``playlist_memberships``). Recorded anyway rather than filtered: a
        filter here would be a second place the FK graph is encoded, and the
        two would drift.
        """
        self.keys.setdefault(table, set()).add(str(pk[0]))

    def release(self, table: str, pk: Sequence[str]) -> None:
        """Record ``pk`` as decided and IN the sync set.

        The affirmative half of :meth:`hold`. Without it a walk that has
        already judged a parent free would re-read it from the database for
        every one of its children.
        """
        self._free.setdefault(table, set()).add(str(pk[0]))

    def blocking_parent(self, table: str, values: Mapping[str, Any]) -> tuple[str, str] | None:
        """The held parent this row references, or None if it is free to go.

        Checks what the walk decided first and the database second, in that
        order: a ``playlists`` row is held for a membership bundle its own
        stamps say nothing about, so the walk's answer is the more complete
        one wherever it exists.
        """
        for parent, column in PARENT_KEYS.get(table, ()):
            value = values.get(column)
            if value is None:
                continue
            key = str(value)
            if key in self.keys.get(parent, set()):
                return parent, key
            if self._stored_parent_is_held(parent, key):
                return parent, key
        return None

    def decide_member_tracks(self, playlist_id: str) -> None:
        """Decide every stored track a playlist's members name, in one read.

        A membership bundle judged in a walk that never visited its tracks
        read each parent with its own statement: 10,042 point reads per
        bundle, three bundles per 10,000-track first sync (LIBM-120 L6). The
        verdict per track is :func:`_stored_row_verdict`, the same one
        :meth:`_stored_parent_is_held` reaches one key at a time, so every
        later member finds its parent already decided. A track no row
        stores is left undecided, and a member naming it is judged exactly
        as before.
        """
        columns = deciding_columns(self._conn, "tracks")
        key_index = columns.index("stable_id")
        decided = self.keys.get("tracks", set()) | self._free.get("tracks", set())
        rows = [
            row
            for row in self._conn.execute(
                f"SELECT {', '.join(columns)} FROM tracks WHERE stable_id IN "
                f"(SELECT stable_id FROM {MEMBERSHIP_TABLE} WHERE playlist_id = ?)",
                (playlist_id,),
            )
            if str(row[key_index]) not in decided
        ]
        self._identity.expect(len(rows))
        for row in rows:
            key = (str(row[key_index]),)
            if _stored_row_verdict(self._conn, "tracks", columns, row, self) is None:
                self.release("tracks", key)
            else:
                self.hold("tracks", key)

    def _stored_parent_is_held(self, parent: str, key: str) -> bool:
        """Judge a parent this walk never visited, by READING it."""
        if key in self._free.get(parent, set()):
            return False
        if (parent, key) in self._resolving:
            # A playlist's own membership bundle names that playlist. The
            # frame above is already deciding it; answering "held" here would
            # make every playlist hold itself.
            return False
        self._resolving.add((parent, key))
        try:
            reason = _stored_row_reason(self._conn, parent, key, self)
        finally:
            self._resolving.discard((parent, key))
        if reason == _MISSING_ROW:
            self._missing.add((parent, key))
            self.hold(parent, (key,))
            return True
        if reason is None:
            self.release(parent, (key,))
            return False
        self.hold(parent, (key,))
        return True


def _stored_row_reason(conn: sqlite3.Connection, table: str, key: str, held: HeldKeys) -> str | None:
    """Why the row stored at ``key`` in parent ``table`` is out, or None.

    A missing row answers :data:`_MISSING_ROW`. SQLite's foreign key does
    not make it impossible: it is enforced only on a connection that turned
    it on, and silver's live library held 209 children of deleted parents.
    The caller holds the child rather than offering it into a FOREIGN KEY 409.
    """
    spec = SPEC_BY_TABLE.get(table)
    if spec is None or len(spec.pk) != 1:
        raise SyncProtocolError(
            f"{table!r} is referenced as a parent by a single column, so its primary key must be a single column too"
        )
    columns = deciding_columns(conn, table)
    row = conn.execute(f"SELECT {', '.join(columns)} FROM {table} WHERE {spec.pk[0]} = ?", (key,)).fetchone()
    if row is None:
        return _MISSING_ROW
    return _stored_row_verdict(conn, table, columns, row, held)


def _stored_row_verdict(
    conn: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
    row: Sequence[Any],
    held: HeldKeys,
) -> str | None:
    """Why this stored parent row, projected to :func:`deciding_columns`, is out."""
    faults = stored_stamp_faults(table, columns, row)
    if faults:
        return describe_faults(faults)
    identity = _track_identity_reason(table, dict(zip(columns, row, strict=True)), held)
    if identity is not None:
        return identity
    if table != "playlists":
        blocked = held.blocking_parent(table, dict(zip(columns, row, strict=True)))
        if blocked is not None:
            if held.is_missing(*blocked):
                suffix = PARENT_MISSING_SUFFIX
            elif held.is_settled(*blocked):
                suffix = SETTLED_PARENT_SUFFIX
            else:
                suffix = PARENT_HELD_SUFFIX
            return f"its {blocked[0]} parent {blocked[1]} {suffix}"
        return None
    return membership_reason(conn, str(row[columns.index(SPEC_BY_TABLE[table].pk[0])]), held)


def row_reason(
    table: str,
    columns: Sequence[str],
    row: Sequence[Any],
    spec: TableSpec,
    held: HeldKeys,
) -> str | None:
    """Why this stored row is NOT in the sync set, or None when it is.

    Records the verdict on ``held`` as a side effect, either way, so a later
    row that references this one is judged against it rather than re-reading
    it. Callers proceed on the affirmative ``None`` -- "every stamp parsed
    and every parent is travelling" -- never on the absence of an exception.
    """
    raw = dict(zip(columns, row, strict=True))
    pk = tuple(str(raw[column]) for column in spec.pk)
    faults = stored_stamp_faults(table, columns, row)
    if faults:
        held.hold(table, pk)
        return describe_faults(faults)
    identity = _track_identity_reason(table, raw, held)
    if identity is not None:
        held.hold(table, pk)
        return identity
    blocked = held.blocking_parent(table, raw)
    if blocked is not None:
        held.hold(table, pk)
        if held.is_missing(*blocked):
            suffix = PARENT_MISSING_SUFFIX
        elif held.is_settled(*blocked):
            suffix = SETTLED_PARENT_SUFFIX
        else:
            suffix = PARENT_HELD_SUFFIX
        return f"its {blocked[0]} parent {blocked[1]} {suffix}"
    held.release(table, pk)
    return None


def excluded_reason(
    conn: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
    row: Sequence[Any],
    spec: TableSpec,
    held: HeldKeys,
) -> str | None:
    """:func:`row_reason` plus the whole-playlist membership rule.

    THE definition of "this stored row is out of the sync set", and the only
    one: the digest hashes what this says is in, the operator count counts
    what it says is out, and a second reading of either would be a number
    that disagrees with the rows.

    A ``playlists`` row is also held when its MEMBERSHIP BUNDLE cannot
    travel, exactly as :func:`apps.sync_hub.engine_changes.spoke_push` holds
    it: membership is whole-playlist, so a playlist whose bundle is
    incomplete never reaches the peer, and hashing it would make the two
    sides disagree about a row neither of them can exchange.
    """
    reason = row_reason(table, columns, row, spec, held)
    if reason is not None or table != "playlists":
        return reason
    # By COLUMN NAME, never by position: nothing guarantees a table's primary
    # key columns come first, and holding the wrong key would exclude a
    # different playlist than the offer held back -- two exclusion sets that
    # look symmetric and are not.
    raw = dict(zip(columns, row, strict=True))
    pk = tuple(str(raw[column]) for column in spec.pk)
    bundle = membership_reason(conn, pk[0], held)
    if bundle is not None:
        held.hold(table, pk)
    return bundle


def spec_for(table: str) -> TableSpec:
    """The :class:`TableSpec` for any table in :data:`FK_ORDER`."""
    if table == MEMBERSHIP_TABLE:
        return MEMBERSHIP_SPEC
    spec = SPEC_BY_TABLE.get(table)
    if spec is None:
        raise SyncProtocolError(f"{table!r} is not in the sync set")
    return spec


def deciding_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    """The only columns :func:`excluded_reason` READS, in declaration order.

    Its primary key, its stamp columns, and the columns naming its parents.
    Everything else in the row is domain data the sync-set predicate never
    looks at, so a caller that only wants the VERDICT can project to these
    and hand the narrow row straight to :func:`excluded_reason` --
    :func:`apps.sync_hub.protocol_common.stored_stamp_faults` pairs columns
    with values positionally, so a projection is as valid as a full row.

    Not for the digest, which HASHES the row and therefore needs all of it.
    ``track_fields`` also projects ``modified_at``: when ``updated_at`` is
    NULL the sync rule orders by that fallback (issue #3101, wire v5), and
    migration v15 backfills ``updated_at`` from it so the fallback is
    transitional.

    This exists because the operator count runs on a polled endpoint: reading
    all 11 columns of every ``tracks`` row to decide a question that turns on
    four of them made that readout several times slower than the stamp-only
    count it replaced.
    """
    spec = spec_for(table)
    wanted = {
        *spec.pk,
        UPDATED_AT,
        DELETED_AT,
        "stable_id_tier",
        "content_hash",
        "isrc",
        *(column for _, column in PARENT_KEYS.get(table, ())),
    }
    if table == "track_fields":
        wanted.add("modified_at")
    return tuple(column for column in table_columns(conn, table) if column in wanted)


def any_stamp_fault(conn: sqlite3.Connection) -> bool:
    """True as soon as ONE stored stamp anywhere cannot be ordered.

    Stamp faults are one root of an exclusion, not the only one: identity
    hold (unidentifiable inferred rows, content-identity duplicates) also
    keeps rows out of the sync set. Use :func:`any_exclusion_root` when the
    question is "is the sync set the whole library?".

    Reads the same :func:`stored_stamp_faults` predicate as everything else
    here, over the stamp columns alone, so this is a cheap way to reach the
    same stamp-only answer and never a second definition of it.
    """
    for table in FK_ORDER:
        stamps = [column for column in deciding_columns(conn, table) if column in (UPDATED_AT, DELETED_AT, MODIFIED_AT)]
        if not stamps:
            continue
        if table == TRACK_FIELDS_TABLE and MODIFIED_AT in stamps:
            where = f"{UPDATED_AT} IS NOT NULL OR ({UPDATED_AT} IS NULL AND {MODIFIED_AT} IS NOT NULL)"
        else:
            where = " OR ".join(f"{column} IS NOT NULL" for column in stamps)
        cursor = conn.execute(f"SELECT {', '.join(stamps)} FROM {table} WHERE {where}")
        for row in cursor:
            if stored_stamp_faults(table, stamps, row):
                return True
    return False


def excluded_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Rows per table this machine holds OUT of the sync set, table -> count.

    One walk of :data:`FK_ORDER` through :func:`excluded_reason`, which is
    the same predicate :func:`apps.sync_hub.protocol.table_digest` hashes
    against. That is what makes the operator readout and the digest's own
    per-table count one computation asked twice rather than two that happen
    to agree -- they disagreed by more than half on the real library before
    this existed, because a second implementation counted stamp faults only
    and knew nothing of the FK cascade or the held membership bundles.

    Tables with nothing excluded are omitted, matching
    :attr:`apps.sync_hub.protocol.SyncDigest.quarantined`.

    The walk is skipped entirely when :func:`any_exclusion_root` says there
    is nothing to root an exclusion in, which is the state an operator is
    told to reach and the one a healthy fleet sits in. This readout is
    polled by the webui cloudsync overview, so that matters: on the repaired
    real library (73,844 sync-set rows) the fast path answers in 42.9ms
    against the 45.0ms the stamp-only count it replaced needed, while
    judging every row to reach the same "none" took 255ms.
    """
    if not any_exclusion_root(conn):
        return {}
    held = HeldKeys(conn)
    counts: dict[str, int] = {}
    for table in FK_ORDER:
        spec = spec_for(table)
        columns = deciding_columns(conn, table)
        order_by = ", ".join(spec.pk)
        cursor = conn.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY {order_by}")
        count = sum(
            1
            for row in cursor
            if (reason := excluded_reason(conn, table, columns, row, spec, held)) is not None and not is_settled(reason)
        )
        if count:
            counts[table] = count
    return counts


def membership_reason(conn: sqlite3.Connection, playlist_id: str, held: HeldKeys) -> str | None:
    """Why this playlist's WHOLE membership bundle cannot travel, or None.

    One bad member holds back the PLAYLIST, not the member. Membership is
    whole-playlist (ADR 04 c5): a bundle with one row dropped tells the peer
    that track was REMOVED from the playlist, and the peer's replace would
    then delete it. Excluding one membership row is therefore not a
    quarantine at all -- it is a silent edit.
    """
    held.decide_member_tracks(playlist_id)
    columns = deciding_columns(conn, MEMBERSHIP_TABLE)
    cursor = conn.execute(
        f"SELECT {', '.join(columns)} FROM {MEMBERSHIP_TABLE} WHERE playlist_id = ? ORDER BY position",
        (playlist_id,),
    )
    for row in cursor:
        reason = row_reason(MEMBERSHIP_TABLE, columns, row, MEMBERSHIP_SPEC, held)
        if reason is not None:
            return f"{MEMBER_HELD_PREFIX} ({reason})"
    return None


def _unsyncable_inferred_candidates(conn: sqlite3.Connection) -> sqlite3.Cursor:
    """Live inferred-tier rows lacking a content hash, ISRC still unchecked.

    Shared by :func:`count_hash_pending` and legacy counters, so the SQL half
    cannot drift apart.
    """
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(tracks)")}
    audio_predicate = "AND (audio_hash IS NULL OR audio_hash = '')" if "audio_hash" in columns else ""
    return conn.execute(
        "SELECT stable_id, stable_id_tier, content_hash, "
        f"{'audio_hash' if 'audio_hash' in columns else 'NULL'}, isrc FROM tracks "
        "WHERE deleted_at IS NULL AND stable_id_tier = 'inferred' "
        "AND (content_hash IS NULL OR content_hash = '') "
        f"{audio_predicate}"
    )


def any_identity_hold(conn: sqlite3.Connection) -> bool:
    """True when at least one live inferred track lacks hash and ISRC."""
    held = HeldKeys(conn)
    for stable_id, tier, content_hash, audio_hash, isrc in _unsyncable_inferred_candidates(conn):
        raw = {
            "stable_id": stable_id,
            "stable_id_tier": tier,
            "content_hash": content_hash,
            "audio_hash": audio_hash,
            "isrc": isrc,
        }
        if _track_identity_reason("tracks", raw, held) == IDENTITY_HOLD_REASON:
            return True
    return False


def any_orphan_child(conn: sqlite3.Connection) -> bool:
    """True when a stored child row names a parent no row stores."""
    for child, refs in PARENT_KEYS.items():
        for parent, column in refs:
            parent_pk = spec_for(parent).pk[0]
            orphan = conn.execute(
                f"SELECT 1 FROM {child} AS c WHERE c.{column} IS NOT NULL AND NOT EXISTS "
                f"(SELECT 1 FROM {parent} AS p WHERE p.{parent_pk} = c.{column}) LIMIT 1"
            ).fetchone()
            if orphan is not None:
                return True
    return False


def any_exclusion_root(conn: sqlite3.Connection) -> bool:
    """True when the sync set is not the whole library."""
    return (
        any_stamp_fault(conn)
        or bool(identity_duplicate_remap(conn))
        or any_identity_hold(conn)
        or any_orphan_child(conn)
    )
