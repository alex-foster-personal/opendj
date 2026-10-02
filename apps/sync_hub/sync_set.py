"""Which STORED rows are in this machine's sync set, and which are held back.

Round 5. One definition, read by both the offer
(:mod:`apps.sync_hub.engine_changes`) and the digest
(:mod:`apps.sync_hub.protocol`), because they must exclude the SAME rows. A
digest that hashes a row the push cannot offer makes two converged peers
disagree forever, which fires the ADR 04 c6 corruption alarm on ordinary
legacy data -- the brick this round removed, wearing a different hat.

Two reasons a row is held back, and the second one is the reason this module
exists rather than a one-line predicate:

1. **Its own stored stamp cannot be ordered.** See
   :mod:`apps.shared.state.sync_stamp` rule 4.
2. **A row it REFERENCES is held back.** Offering a child whose parent the
   peer does not have makes the peer answer the whole chunk with
   ``FOREIGN KEY constraint failed`` -- an HTTP 409 that re-fires on every
   retry forever, which is round 1 finding 1's exact shape one table over.

So one legacy stamp on a track costs that track AND its dependants. That is a
bigger blast radius than one row, and it is stated rather than hidden: the
count reports the total, every exclusion is logged with its cause, and one
run of ``python -m apps.shared.state.normalize_stamps --live`` clears all of
it.

**Three walks, one accumulator that reads.** The transitive rule has to hold
in every walk that decides exclusions, and only one of them sees every row: a
FULL offer (and the digest) covers all of :data:`FK_ORDER` parents-first, but
a FENCED offer covers only the rows a changelog window names, and a hub PULL
covers one chunk of that window at a time. In those two the held parent is
routinely OUTSIDE the walk, so an accumulator that only remembers what the
walk showed it answers "free to go" for a parent it never looked at.
:class:`HeldKeys` therefore takes a connection and READS any parent the walk
has not judged, caching both verdicts. That is why it has one behavior rather
than a flag: a walk-scoped mode is the defect, and a mode nobody selects
cannot be selected wrongly.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from apps.shared.state.ids import normalise_isrc
from apps.sync_hub.protocol_common import (
    DELETED_AT,
    MEMBERSHIP_SPEC,
    MEMBERSHIP_TABLE,
    MODIFIED_AT,
    ORIGIN_DEVICE_ID,
    SPEC_BY_TABLE,
    SYNC_TABLES,
    TRACK_FIELDS_TABLE,
    UPDATED_AT,
    StampFault,
    SyncProtocolError,
    TableSpec,
    describe_faults,
    is_hash_pending_candidate,
    lww_key,
    stored_stamp_faults,
    table_columns,
)

IDENTITY_HOLD_REASON: str = (
    "inferred-tier track has no content_hash, no audio_hash, and no "
    "normalizable ISRC; offering it would mint a path-tier PK the hub "
    "cannot collapse"
)
IDENTITY_DUP_REASON: str = "content-identity duplicate; the LWW survivor is another tracks row"
#: A duplicate the HUB has already answered for (a persisted
#: ``sync_identity_remap`` names it a loser, CLOUDSYNC-32). Still never
#: offered, but SETTLED: not counted as held and never a fence pin, because
#: no repair on this machine will ever make it travel under its own id.
IDENTITY_SETTLED_REASON: str = (
    "content-identity duplicate the hub already collapsed into another tracks row; settled"
)
#: The fixed tail of a child of a settled duplicate: it stays with its parent.
SETTLED_PARENT_SUFFIX: str = "is a settled content-identity duplicate"
#: The fixed tail of every "held because its parent is held" reason, and the
#: fixed head of every "held because a member is held" one. Only the one
#: row's parent or member varies between them, which is what
#: :func:`hold_group` takes out.
PARENT_HELD_SUFFIX: str = (
    "is itself held back, and offering a child without its parent is a "
    "FOREIGN KEY 409 no retry gets past"
)
MEMBER_HELD_PREFIX: str = "a track in it is not in the sync set"

#: Every parent key a sync-set row must not outrun, as
#: ``child table -> ((parent table, referencing column), ...)``. Mirrors the
#: ``REFERENCES`` clauses in ``apps/shared/state/schema.py``; pinned against
#: them by ``tests/cloudsync/test_hub_sync_quarantine.py``.
PARENT_KEYS: dict[str, tuple[tuple[str, str], ...]] = {
    "lyric_verdict": (("tracks", "stable_id"),),
    "track_vendor_ids": (("tracks", "stable_id"),),
    "track_fields": (("tracks", "stable_id"),),
    "track_locations": (("tracks", "stable_id"),),
    "playlist_pins": (("playlists", "playlist_id"),),
    MEMBERSHIP_TABLE: (("tracks", "stable_id"), ("playlists", "playlist_id")),
}

#: Every digest/offer table, parents before children. ``SYNC_TABLES`` is
#: already the FK-safe apply order; membership goes last because it
#: references both ``tracks`` and ``playlists``. Any walk that decides
#: exclusions MUST use this order, or a child is judged before its parent is
#: known to be held.
FK_ORDER: tuple[str, ...] = (
    *(spec.name for spec in SYNC_TABLES),
    MEMBERSHIP_TABLE,
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
        from apps.sync_hub.identity_verdicts import IdentityLoserVerdicts

        self._identity = IdentityLoserVerdicts(conn)

    def is_identity_loser(self, stable_id: str) -> bool:
        """True when this ``tracks`` row loses an identity election to another row.

        Decided per row, from the rows sharing its identity keys, rather than
        by reading the whole library when the walk starts: a hub pull page is
        one walk, and a first sync of 10,000 tracks read the library once per
        page (LIBM-120 L6).
        """
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

    def is_settled(self, table: str, key: str) -> bool:
        """True when ``table``/``key`` was held as a settled duplicate in this walk."""
        return (table, key) in self._settled

    def hold(self, table: str, pk: Sequence[str]) -> None:
        """Record ``pk``'s first column as held under ``table``.

        Only ``tracks`` and ``playlists`` are ever looked up again (they are
        the only parents in :data:`PARENT_KEYS`), so an entry for any other
        table is inert. Recorded anyway rather than filtered: a filter here
        would be a second place the FK graph is encoded, and the two would
        drift.
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
        if reason is None:
            self.release(parent, (key,))
            return False
        self.hold(parent, (key,))
        return True


def _stored_row_reason(
    conn: sqlite3.Connection, table: str, key: str, held: HeldKeys
) -> str | None:
    """Why the row stored at ``key`` in parent ``table`` is out, or None.

    A missing row is NOT held: the child could not reference a parent this
    database does not have, because SQLite's own foreign key would have
    refused the write. Reporting it as held would invent an exclusion out of
    a row that cannot exist.
    """
    spec = SPEC_BY_TABLE.get(table)
    if spec is None or len(spec.pk) != 1:
        raise SyncProtocolError(
            f"{table!r} is referenced as a parent by a single column, so its "
            f"primary key must be a single column too"
        )
    columns = deciding_columns(conn, table)
    row = conn.execute(
        f"SELECT {', '.join(columns)} FROM {table} WHERE {spec.pk[0]} = ?", (key,)
    ).fetchone()
    if row is None:
        return None
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
        suffix = SETTLED_PARENT_SUFFIX if held.is_settled(*blocked) else PARENT_HELD_SUFFIX
        return f"its {blocked[0]} parent {blocked[1]} {suffix}"
    held.release(table, pk)
    return None


def is_settled(reason: str | None) -> bool:
    """True when ``reason`` excludes a row the hub already settled (CLOUDSYNC-32).

    Such a row is still never offered, but it is not HELD: it is not counted
    as excluded, not re-logged after a full offer and not a push-fence pin.
    """
    return reason is not None and exclusion_kind(reason) == "identity_settled"


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
        stamps = [
            column
            for column in deciding_columns(conn, table)
            if column in (UPDATED_AT, DELETED_AT, MODIFIED_AT)
        ]
        if not stamps:
            continue
        if table == TRACK_FIELDS_TABLE and MODIFIED_AT in stamps:
            where = (
                f"{UPDATED_AT} IS NOT NULL OR ({UPDATED_AT} IS NULL AND {MODIFIED_AT} IS NOT NULL)"
            )
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
    is nothing to root an exclusion in. The webui cloudsync overview polls
    this readout, so the fast path avoids judging every stored row when no
    exclusion root exists. Historical latency comparisons used private
    library inputs that are unavailable with this source; no reproducible
    latency figure or speedup is claimed here.
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
            if (reason := excluded_reason(conn, table, columns, row, spec, held)) is not None
            and not is_settled(reason)
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
        f"SELECT {', '.join(columns)} FROM {MEMBERSHIP_TABLE} "
        f"WHERE playlist_id = ? ORDER BY position",
        (playlist_id,),
    )
    for row in cursor:
        reason = row_reason(MEMBERSHIP_TABLE, columns, row, MEMBERSHIP_SPEC, held)
        if reason is not None:
            return f"{MEMBER_HELD_PREFIX} ({reason})"
    return None


def hold_group(reason: str) -> str:
    """``reason`` with the one row's own parent or member taken out.

    Rows held for the same cause share this grouping, so a report can count
    them without producing a distinct reason for every parent identifier.
    Every other reason remains verbatim, so an offending stored value still
    reaches the log.
    """
    if reason.startswith(MEMBER_HELD_PREFIX):
        return MEMBER_HELD_PREFIX
    if reason.startswith("its ") and reason.endswith(PARENT_HELD_SUFFIX):
        parent_table = reason.split(" ", 2)[1]
        return f"its {parent_table} parent {PARENT_HELD_SUFFIX}"
    return reason


def _invert_pk(pk: str) -> str:
    """Sort key so ``max`` prefers the smaller PK on a stamp tie."""
    return "".join(chr(255 - ord(ch)) for ch in pk)


IdentityRow = Sequence[Any]
"""``(stable_id, content_hash, audio_hash, isrc, updated_at, origin_device_id)``."""


@dataclass(frozen=True)
class IdentityKeys:
    """The election keys one ``tracks`` row files under, as hub collapse reads them."""

    hashes: tuple[str, ...]
    isrc: str | None


def identity_keys(content_hash: object, audio_hash: object, isrc: object) -> IdentityKeys:
    """``content_hash`` then a distinct ``audio_hash``; a normalizable ISRC only without either."""
    digest = str(content_hash).strip() if content_hash else ""
    audio_digest = str(audio_hash).strip() if audio_hash else ""
    hashes = tuple(
        key for key in (digest, audio_digest if audio_digest != digest else "") if key
    )
    norm = normalise_isrc(None if isrc is None else str(isrc))
    return IdentityKeys(hashes=hashes, isrc=None if hashes else norm)


def identity_row_select(conn: sqlite3.Connection) -> str:
    """``SELECT`` of :data:`IdentityRow` over live tracks, before any further filter."""
    audio_column = (
        "audio_hash" if "audio_hash" in table_columns(conn, "tracks") else "NULL AS audio_hash"
    )
    return (
        f"SELECT stable_id, content_hash, {audio_column}, isrc, "
        "updated_at, origin_device_id FROM tracks WHERE deleted_at IS NULL"
    )


def identity_duplicate_remap(conn: sqlite3.Connection) -> dict[str, str]:
    """Loser PK -> LWW survivor PK for live tracks sharing a merge key.

    Same keys as hub collapse: ``content_hash`` or ``audio_hash`` first, then a normalizable
    ISRC. Path-tier ``stable_id`` is never a merge key. Groups whose identity
    signals conflict (same hash, disagreeing ISRC) are left unmapped --
    apply-time quarantine owns those.
    """
    return identity_remap_from_rows(conn.execute(identity_row_select(conn)).fetchall())


def identity_remap_from_rows(rows: Iterable[IdentityRow]) -> dict[str, str]:
    """:func:`identity_duplicate_remap` over ``rows`` alone.

    Exact for every row whose identity keys no row outside ``rows`` shares.
    """
    by_hash: dict[str, list[tuple[str, str | None, tuple[str, str]]]] = {}
    by_isrc: dict[str, list[tuple[str, str | None, tuple[str, str]]]] = {}
    stamp_cols = (UPDATED_AT, ORIGIN_DEVICE_ID)
    for pk, content_hash, audio_hash, isrc, updated_at, origin in rows:
        if stored_stamp_faults("tracks", stamp_cols, (updated_at, origin)):
            continue
        sid = str(pk)
        norm = normalise_isrc(None if isrc is None else str(isrc))
        key = lww_key({UPDATED_AT: updated_at, ORIGIN_DEVICE_ID: origin})
        keys = identity_keys(content_hash, audio_hash, isrc)
        for digest in keys.hashes:
            by_hash.setdefault(digest, []).append((sid, norm, key))
        if keys.isrc:
            by_isrc.setdefault(keys.isrc, []).append((sid, None, key))
    remap: dict[str, str] = {}
    _add_identity_group_remaps(by_hash.values(), remap, preserve_existing=False)
    _add_identity_group_remaps(by_isrc.values(), remap, preserve_existing=True)
    return remap


def _add_identity_group_remaps(
    groups: Iterable[list[tuple[str, str | None, tuple[str, str]]]],
    remap: dict[str, str],
    *,
    preserve_existing: bool,
) -> None:
    """Add safe loser-to-champion mappings for one identity-key family."""
    for group in groups:
        if len(group) < 2:
            continue
        values = {item[1] for item in group if item[1]}
        if len(values) > 1:
            continue
        champion = max(group, key=lambda item: (item[2], _invert_pk(item[0])))
        for sid, _, _ in group:
            if sid != champion[0] and (not preserve_existing or sid not in remap):
                remap[sid] = champion[0]


def _track_identity_reason(table: str, raw: Mapping[str, Any], held: HeldKeys) -> str | None:
    """Why this ``tracks`` row is held for identity, or None."""
    if table != "tracks":
        return None
    pk = str(raw.get("stable_id") or "")
    loser = held.loser_reason(pk)
    if loser is not None:
        return loser
    if is_hash_pending_track(raw, held):
        return None
    if str(raw.get("stable_id_tier") or "") != "inferred":
        return None
    digest = raw.get("content_hash")
    if digest is not None and str(digest).strip():
        return None
    audio_digest = raw.get("audio_hash")
    if audio_digest is not None and str(audio_digest).strip():
        return None
    if normalise_isrc(None if raw.get("isrc") is None else str(raw.get("isrc"))):
        return None
    return IDENTITY_HOLD_REASON


def is_hash_pending_track(raw: Mapping[str, Any], held: HeldKeys | None = None) -> bool:
    """True when this live ``tracks`` row should travel with ``hash_pending``."""
    if held is not None:
        pk = str(raw.get("stable_id") or "")
        if held.is_identity_loser(pk):
            return False
    return is_hash_pending_candidate("tracks", raw)


def _unsyncable_inferred_candidates(conn: sqlite3.Connection) -> sqlite3.Cursor:
    """Live inferred-tier rows lacking a content hash, ISRC still unchecked.

    Shared by :func:`count_hash_pending` and legacy counters, so the SQL half
    cannot drift apart.
    """
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(tracks)")}
    audio_predicate = (
        "AND (audio_hash IS NULL OR audio_hash = '')" if "audio_hash" in columns else ""
    )
    return conn.execute(
        "SELECT stable_id, stable_id_tier, content_hash, "
        f"{'audio_hash' if 'audio_hash' in columns else 'NULL'}, isrc FROM tracks "
        "WHERE deleted_at IS NULL AND stable_id_tier = 'inferred' "
        "AND (content_hash IS NULL OR content_hash = '') "
        f"{audio_predicate}"
    )


def count_hash_pending(conn: sqlite3.Connection) -> int:
    """How many live tracks would travel with ``hash_pending: true``.

    Mirrors :func:`is_hash_pending_track` over live rows, excluding identity-dup
    losers. Cheap indexed-ish scan, no file I/O.
    """
    held = HeldKeys(conn)
    count = 0
    for stable_id, tier, content_hash, audio_hash, isrc in _unsyncable_inferred_candidates(conn):
        raw = {
            "stable_id": stable_id,
            "stable_id_tier": tier,
            "content_hash": content_hash,
            "audio_hash": audio_hash,
            "isrc": isrc,
        }
        if is_hash_pending_track(raw, held):
            count += 1
    return count


def count_unsyncable_inferred(conn: sqlite3.Connection) -> int:
    """How many live tracks :func:`_track_identity_reason` would still hold.

    After ADR-0068 this is identity-dup losers only (hash_pending rows travel).
    """
    held = HeldKeys(conn)
    count = 0
    for stable_id, tier, content_hash, audio_hash, isrc in _unsyncable_inferred_candidates(conn):
        raw = {
            "stable_id": stable_id,
            "stable_id_tier": tier,
            "content_hash": content_hash,
            "audio_hash": audio_hash,
            "isrc": isrc,
        }
        reason = _track_identity_reason("tracks", raw, held)
        if reason == IDENTITY_DUP_REASON:
            count += 1
    return count


ExclusionKind = Literal[
    "stamp", "identity_hold", "identity_dup", "identity_settled", "parent_held", "membership_held"
]


@dataclass(frozen=True)
class StampFaultRow:
    """One row held for a direct stamp fault, with every offending column."""

    table: str
    pk: tuple[str, ...]
    faults: tuple[StampFault, ...]


def exclusion_kind(reason: str) -> ExclusionKind:
    """Classify one exclusion reason for operator messaging."""
    if IDENTITY_SETTLED_REASON in reason or reason.endswith(SETTLED_PARENT_SUFFIX):
        return "identity_settled"
    if IDENTITY_HOLD_REASON in reason:
        return "identity_hold"
    if IDENTITY_DUP_REASON in reason:
        return "identity_dup"
    if reason.startswith(MEMBER_HELD_PREFIX):
        return "membership_held"
    if reason.startswith("its ") and PARENT_HELD_SUFFIX in reason:
        return "parent_held"
    if ".updated_at" in reason or ".deleted_at" in reason:
        return "stamp"
    return "stamp"


def remedy_for(reason: str) -> str:
    """Operator next step for one hold reason; normalize_stamps only for stamps."""
    kind = exclusion_kind(reason)
    if kind == "identity_hold":
        return (
            "it carries identity (`python -m apps.shared.state."
            "backfill_content_hash --live` or `/fix-links`)"
        )
    if kind == "identity_dup":
        return "the LWW survivor of this content identity is offered instead"
    if kind == "identity_settled":
        return "nothing to repair: the hub keeps the survivor of this content identity"
    if kind in ("parent_held", "membership_held"):
        return (
            f"the root hold must be cleared first ({hold_group(reason)}); "
            "see the group's ERROR line for the root remedy"
        )
    return "it is repaired with `python -m apps.shared.state.normalize_stamps --live`"


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


def stamp_fault_rows(conn: sqlite3.Connection) -> list[StampFaultRow]:
    """Every row with a direct stamp fault, for the normalizer bridge test."""
    if not any_stamp_fault(conn):
        return []
    rows: list[StampFaultRow] = []
    for table in FK_ORDER:
        spec = spec_for(table)
        columns = deciding_columns(conn, table)
        order_by = ", ".join(spec.pk)
        cursor = conn.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY {order_by}")
        for row in cursor:
            faults = stored_stamp_faults(table, columns, row)
            if not faults:
                continue
            raw = dict(zip(columns, row, strict=True))
            pk = tuple(str(raw[column]) for column in spec.pk)
            rows.append(StampFaultRow(table=table, pk=pk, faults=faults))
    return rows


def exclusion_counts_by_kind(conn: sqlite3.Connection) -> dict[ExclusionKind, int]:
    """Rows outside the sync set, bucketed by :func:`exclusion_kind`."""
    if not any_exclusion_root(conn):
        return {}
    held = HeldKeys(conn)
    counts: dict[ExclusionKind, int] = {}
    for table in FK_ORDER:
        spec = spec_for(table)
        columns = deciding_columns(conn, table)
        order_by = ", ".join(spec.pk)
        cursor = conn.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY {order_by}")
        for row in cursor:
            reason = excluded_reason(conn, table, columns, row, spec, held)
            if reason is None:
                continue
            kind = exclusion_kind(reason)
            counts[kind] = counts.get(kind, 0) + 1
    return counts


def count_quarantined_roots(conn: sqlite3.Connection) -> int:
    """Direct stamp faults and identity-dup holds only (CSSTATUS-07)."""
    if not any_exclusion_root(conn):
        return 0
    held = HeldKeys(conn)
    count = 0
    for table in FK_ORDER:
        spec = spec_for(table)
        columns = deciding_columns(conn, table)
        order_by = ", ".join(spec.pk)
        cursor = conn.execute(f"SELECT {', '.join(columns)} FROM {table} ORDER BY {order_by}")
        for row in cursor:
            reason = row_reason(table, columns, row, spec, held)
            if reason is None:
                continue
            if exclusion_kind(reason) in ("stamp", "identity_dup"):
                count += 1
    return count


def excluded_total(conn: sqlite3.Connection) -> int:
    """Every row this machine holds outside the sync set."""
    return sum(excluded_counts(conn).values())


def inconclusive_remedy(
    conn: sqlite3.Connection,
    *,
    held_here: int = 0,
    hub_quarantined: int | None = None,
) -> str:
    """Next-step line for inconclusive digest; normalize_stamps only when warranted."""
    if stamp_fault_rows(conn):
        return (
            "Run `python -m apps.shared.state.normalize_stamps --live` on this "
            "machine, then sync again."
        )
    if held_here == 0 and hub_quarantined:
        return (
            f"The hub holds {hub_quarantined} row(s) outside its sync set; "
            "repair them on the hub per hub engine-warn.log "
            "(``python -m apps.shared.state.normalize_stamps --live`` for "
            "stamp faults, ``python -m apps.shared.state.backfill_content_hash "
            "--live`` or ``/fix-links`` for identity holds, or resolve "
            "identity-duplicate losers), then sync again."
        )
    by_kind = exclusion_counts_by_kind(conn)
    if by_kind.get("identity_hold"):
        return (
            "Run `python -m apps.shared.state.backfill_content_hash --live` "
            "or `/fix-links` on machines holding identity-blocked tracks, "
            "then sync again."
        )
    if by_kind.get("identity_dup"):
        return (
            "Identity-duplicate losers are excluded locally; ensure both sides "
            "run the same build and sync again."
        )
    if by_kind.get("parent_held") or by_kind.get("membership_held"):
        return (
            "Transitive holds remain from a root exclusion; repair the root "
            "cause named in engine-warn.log, then sync again."
        )
    return "Repair exclusions named in engine-warn.log, then sync again."


def format_inconclusive_exclusion_summary(
    conn: sqlite3.Connection,
    held_here: int,
    held_hub: str | int,
    *,
    hub_quarantined: int | None = None,
) -> str:
    """Human-readable exclusion breakdown for inconclusive journal copy."""
    by_kind = exclusion_counts_by_kind(conn)
    parts = [
        f"{by_kind[kind]} {kind.replace('_', '-')}"
        for kind in (
            "stamp",
            "identity_dup",
            "identity_hold",
            "parent_held",
            "membership_held",
        )
        if by_kind.get(kind)
    ]
    breakdown = ", ".join(parts) if parts else "no root causes classified"
    return (
        f"EXCLUDED rows on at least one side ({held_here} held here, "
        f"{held_hub} on the hub): {breakdown}. "
        f"{inconclusive_remedy(conn, held_here=held_here, hub_quarantined=hub_quarantined)}"
    )


def any_exclusion_root(conn: sqlite3.Connection) -> bool:
    """True when the sync set is not the whole library."""
    return any_stamp_fault(conn) or bool(identity_duplicate_remap(conn)) or any_identity_hold(conn)


__all__ = [
    "FK_ORDER",
    "IDENTITY_DUP_REASON",
    "IDENTITY_HOLD_REASON",
    "IDENTITY_SETTLED_REASON",
    "MEMBER_HELD_PREFIX",
    "PARENT_HELD_SUFFIX",
    "PARENT_KEYS",
    "SETTLED_PARENT_SUFFIX",
    "ExclusionKind",
    "HeldKeys",
    "IdentityKeys",
    "IdentityRow",
    "StampFaultRow",
    "any_exclusion_root",
    "any_identity_hold",
    "any_stamp_fault",
    "count_hash_pending",
    "count_quarantined_roots",
    "count_unsyncable_inferred",
    "deciding_columns",
    "excluded_counts",
    "excluded_reason",
    "excluded_total",
    "exclusion_counts_by_kind",
    "exclusion_kind",
    "format_inconclusive_exclusion_summary",
    "identity_duplicate_remap",
    "identity_keys",
    "identity_remap_from_rows",
    "identity_row_select",
    "inconclusive_remedy",
    "is_hash_pending_track",
    "is_settled",
    "membership_reason",
    "remedy_for",
    "row_reason",
    "spec_for",
    "stamp_fault_rows",
]
