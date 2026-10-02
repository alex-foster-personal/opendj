"""Sync-set definitions and canonical row serialization.

Split out of :mod:`apps.sync_hub.protocol` (quality-gate file_size ratchet,
round 4): the table specs, the canonicalization rules (module docstring
points 4 and 5 there), schema introspection and the LWW sort key are the
foundation everything else in the package reads -- the wire payload
dataclasses, the digest, and the merge engine. Kept dependency-free of the
rest of ``apps.sync_hub`` so nothing here can accidentally import back the
other direction.

**Two directions, one canonicalizer** (round 5). :func:`canonical_values`
reads a row OFF THE WIRE; :func:`canonical_row` reads one OUT OF A LOCAL
TABLE. Both are strict and both raise :class:`SyncProtocolError` on a
timestamp that cannot be placed on the UTC line, and that is deliberate: a
raise from either remains a genuine bug rather than a legacy row to absorb.
The directions differ in WHO ASKS FIRST.

* Off the wire there is nothing to ask. A peer sending an unorderable stamp
  is a protocol violation and the strictness IS the answer.
* Off a local table the caller asks :func:`stored_stamp_faults` first and
  QUARANTINES the row it names -- excludes that one row from the sync set,
  counts it, and logs the offending value. Round 4 had no such question, so
  ``canonical_row`` raised mid-selection and one legacy row aborted every
  push and every digest on the machine, forever (round 3 finding N3b's
  unfixed half).

The predicate is affirmative on purpose: an empty tuple means every stamp
column PARSED, which is a fact a caller can proceed on. "No exception was
raised" is not (``.claude/rules/verification.md``).
"""
from __future__ import annotations

import json
import sqlite3
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from apps.shared.state import sync_stamp
from apps.shared.state.ids import normalise_isrc

# A row whose ``updated_at`` is NULL predates migration v6. ADR 04 c7 says it
# syncs as epoch-old. This literal sorts before any real ISO8601 timestamp
# under plain string comparison, which is the only ordering the protocol uses.
EPOCH: str = "0000-01-01T00:00:00+00:00"

# Same idea for a NULL origin: it must never win the tiebreak.
NO_ORIGIN: str = ""


class SyncProtocolError(ValueError):
    """A payload could not be read as this protocol. Never swallowed."""


# ----- table specs -----------------------------------------------------------


@dataclass(frozen=True)
class TableSpec:
    """One table in the LWW sync set."""

    name: str
    pk: tuple[str, ...]


# Order is the APPLY order and is FK-safe: parents before children. Callers
# must not reorder without re-checking the REFERENCES clauses in
# apps/shared/state/schema.py.
SYNC_TABLES: tuple[TableSpec, ...] = (
    TableSpec("tracks", ("stable_id",)),
    # Immediately after its parent: lyric_verdict's only REFERENCES clause is
    # to tracks(stable_id), so slot 1 keeps the parent adjacency the comment
    # above describes.
    TableSpec("lyric_verdict", ("stable_id",)),
    TableSpec("playlists", ("playlist_id",)),
    TableSpec("track_vendor_ids", ("stable_id", "vendor")),
    TableSpec("track_fields", ("stable_id", "field_name")),
    TableSpec("track_locations", ("location_id",)),
    TableSpec("sync_policies", ("machine_id", "asset_kind")),
    TableSpec("playlist_pins", ("machine_id", "playlist_id")),
    # v12 (FBSYNC-01, ADR-0013). No REFERENCES clause, so its slot in the
    # apply order is free; last keeps every existing table's rank unchanged.
    TableSpec("feedback_pins", ("pin_id",)),
)

SPEC_BY_TABLE: dict[str, TableSpec] = {spec.name: spec for spec in SYNC_TABLES}

# Where a table's LOGICAL identity is not its primary key, the natural key is
# the tuple its partial UNIQUE indexes assert (ADR 08 point 1). Only
# ``track_locations`` has one: ``location_id`` is a random uuid minted
# independently on each machine, so two machines can hold the same logical row
# under different keys. The engine resolves against this tuple, which is what
# makes round 1 finding 1 (a UNIQUE violation the push could never retry past)
# unreachable rather than merely unlikely.
#
# ``lyric_verdict`` (v10) gets NO entry, and that is the convention rather than
# an omission: its primary key ``stable_id`` IS its logical identity (one row
# per track, keyed by a value both machines derive rather than mint), and it
# carries no UNIQUE index for a natural key to be read off.
#
# EVERY tuple whose columns are all non-NULL is in force at once, mirroring
# the ``WHERE file_path IS NOT NULL`` / ``WHERE remote_url IS NOT NULL``
# partial indexes: a row carrying both columns is subject to both indexes.
# SQLite treats NULLs as distinct in a UNIQUE index, so a NULL anywhere in a
# tuple means that index does not apply and there is nothing to resolve
# against for it. Round 2 finding N5: resolving against the FIRST applicable
# tuple only left the other index unresolved, and its violation was round 1
# finding 1's exact shape one index over -- a 409 that re-fired forever.
NATURAL_KEYS: dict[str, tuple[tuple[str, ...], ...]] = {
    "track_locations": (
        ("stable_id", "machine_id", "kind", "file_path"),
        ("stable_id", "machine_id", "kind", "remote_url"),
    ),
}

# Whole-playlist granularity (ADR 04 c5): membership rows are never pushed on
# their own. They travel attached to their ``playlists`` row and replace the
# peer's copy wholesale when that row wins LWW. A writer that edits membership
# MUST stamp ``playlists.updated_at`` or the change will not propagate.
MEMBERSHIP_TABLE: str = "playlist_memberships"
MEMBERSHIP_SPEC: TableSpec = TableSpec(MEMBERSHIP_TABLE, ("playlist_id", "position"))

# Fleet registry. See point 3 in :mod:`apps.sync_hub.protocol`'s docstring.
REGISTRY_TABLE: str = "machines"

# The digest set: every table whose content must be byte-identical on two
# converged peers. Tombstones are included -- a peer that has forgotten a
# delete has diverged, and the digest is the only thing that would say so.
DIGEST_TABLES: tuple[str, ...] = tuple(
    sorted([spec.name for spec in SYNC_TABLES] + [MEMBERSHIP_TABLE])
)

# Columns that carry sync semantics rather than domain data.
UPDATED_AT: str = "updated_at"
ORIGIN_DEVICE_ID: str = "origin_device_id"
DELETED_AT: str = "deleted_at"
MODIFIED_AT: str = "modified_at"
TRACK_FIELDS_TABLE: str = "track_fields"
#: ``tracks`` only: the stamp of the last explicit restore (schema v23). With
#: ``deleted_at`` it forms the row's lifecycle key, which outranks
#: ``updated_at`` in a merge (:func:`lifecycle_key`, wire v7).
RESTORED_AT: str = "restored_at"
TRACKS_TABLE: str = "tracks"
SYNC_COLUMNS: tuple[str, ...] = (UPDATED_AT, ORIGIN_DEVICE_ID, DELETED_AT)


# ----- canonical serialization ------------------------------------------------


def canonical_bytes(payload: Any) -> bytes:
    """Formatting-independent JSON bytes, matching the crate-sync precedent.

    Mirrors ``apps.agentbox.crate_state._canonical_bytes`` so two digests in
    this repo cannot disagree about what "canonical" means.
    """
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


def nfc(value: str) -> str:
    """NFC-normalize one string (module docstring point 5)."""
    return unicodedata.normalize("NFC", value)


def canonical_timestamp(table: str, column: str, value: Any) -> str:
    """Re-emit one timestamp in the single canonical format, or raise.

    Module docstring point 4. Delegates to
    :mod:`apps.shared.state.sync_stamp` so the format has exactly one
    definition in the repo, and converts its error type so every caller in
    this module raises :class:`SyncProtocolError`.
    """
    try:
        return sync_stamp.to_canonical(str(value))
    except sync_stamp.SyncStampError as exc:
        raise SyncProtocolError(
            f"{table}.{column} = {value!r} is not an orderable timestamp: "
            f"{exc}. Sync stamps must be offset-bearing ISO8601; see "
            f"apps.shared.state.sync_stamp.CANONICAL_FORMAT."
        ) from exc


@dataclass(frozen=True)
class StampFault:
    """One STORED value in a stamp column the protocol cannot order.

    Carries the value, not just a count, because the operator report and the
    log line have to name the table, column and offending value rather
    than report only a count; otherwise the repair is a guessing game.
    """

    table: str
    column: str
    value: Any

    def describe(self) -> str:
        return f"{self.table}.{self.column} = {self.value!r}"


def stored_stamp_faults(
    table: str, columns: Sequence[str], row: Sequence[Any]
) -> tuple[StampFault, ...]:
    """Every stamp column of this STORED row the protocol cannot order.

    Empty means every stamp column parsed. Callers proceed on that
    affirmative, never on "no exception was raised".

    Only :data:`UPDATED_AT` and :data:`DELETED_AT` are examined, and a NULL
    in either is orderable by construction (ADR 04 c7 reads it as epoch-old),
    so it is not a fault -- except on ``track_fields``, where a NULL
    ``updated_at`` falls back to :data:`MODIFIED_AT` for ordering (issue
    #3101) and an unorderable fallback is a fault. Deliberately NOT a general
    "would ``canonical_row``
    raise?" probe: the other way that function raises is a BLOB or a column
    count mismatch, and those mean the schema grew something this protocol
    cannot transport. Quarantining a row for that would hide a schema defect
    behind a per-row counter, so those still raise.
    """
    if len(columns) != len(row):
        raise SyncProtocolError(
            f"{table}: {len(columns)} columns declared but {len(row)} values read"
        )
    values = dict(zip(columns, row, strict=True))
    faults: list[StampFault] = []
    for column, value in values.items():
        if column == DELETED_AT:
            if value is None:
                continue
            if not sync_stamp.is_orderable(str(value)):
                faults.append(StampFault(table=table, column=column, value=value))
            continue
        if column != UPDATED_AT:
            continue
        if value is not None:
            if not sync_stamp.is_orderable(str(value)):
                faults.append(StampFault(table=table, column=column, value=value))
            continue
        if table != TRACK_FIELDS_TABLE:
            continue
        fallback = values.get(MODIFIED_AT)
        if fallback is None:
            continue
        if not sync_stamp.is_orderable(str(fallback)):
            faults.append(
                StampFault(table=table, column=MODIFIED_AT, value=fallback)
            )
    return tuple(faults)


def describe_faults(faults: Sequence[StampFault]) -> str:
    """One log-ready line naming every fault, first one first."""
    return "; ".join(fault.describe() for fault in faults)


def pk_columns(table: str) -> tuple[str, ...]:
    """Primary key columns of a sync-set (or membership) table."""
    spec = SPEC_BY_TABLE.get(table)
    if spec is not None:
        return spec.pk
    if table == MEMBERSHIP_TABLE:
        return MEMBERSHIP_SPEC.pk
    if table == REGISTRY_TABLE:
        return ("machine_id",)
    raise SyncProtocolError(f"{table!r} is not in the sync set")


def _checked_value(table: str, column: str, value: Any, *, is_pk: bool) -> Any:
    """Return ``value`` as a canonical JSON-safe scalar, or raise.

    Three normalizations, all of them load-bearing:

    * BLOB columns have no place in the sync set; one appearing means the
      schema grew a column this protocol cannot transport, and guessing an
      encoding would put unreadable rows on the wire.
    * ``updated_at`` AND ``deleted_at`` are re-emitted canonical (point 4).
      Round 3 finding R8 caught the asymmetry: this special-cased
      ``updated_at`` only, so a push carrying ``deleted_at: "not-a-timestamp"``
      was accepted, stored verbatim and hashed into the digest -- the one
      column a tombstone's correctness depends on was the one column this
      boundary never checked. A NULL value (not deleted) still short-circuits
      above and is never passed here, so this only ever validates a REAL
      tombstone stamp.
    * every other non-pk string is NFC-normalized (point 5).
    """
    if value is None:
        return None
    if not isinstance(value, (str, int, float, bool)):
        raise SyncProtocolError(
            f"{table}.{column} holds {type(value).__name__}, which the sync wire "
            f"format cannot carry (only null/text/integer/real)."
        )
    if (
        column in (UPDATED_AT, DELETED_AT)
        or (column == MODIFIED_AT and table == TRACK_FIELDS_TABLE)
        or (column == RESTORED_AT and table == TRACKS_TABLE)
    ):
        return canonical_timestamp(table, column, value)
    if isinstance(value, str) and not is_pk:
        return nfc(value)
    return value


def canonical_row(
    table: str, columns: Sequence[str], row: Sequence[Any]
) -> dict[str, Any]:
    """One DB row as a column->scalar mapping, ready to hash or serialize."""
    if len(columns) != len(row):
        raise SyncProtocolError(
            f"{table}: {len(columns)} columns declared but {len(row)} values read"
        )
    keys = set(pk_columns(table))
    return {
        column: _checked_value(table, column, value, is_pk=column in keys)
        for column, value in zip(columns, row, strict=True)
    }


def canonical_values(table: str, values: Mapping[str, Any]) -> dict[str, Any]:
    """:func:`canonical_row` for a mapping that arrived off the wire."""
    keys = set(pk_columns(table))
    return {
        column: _checked_value(table, column, value, is_pk=column in keys)
        for column, value in values.items()
    }


def natural_keys(
    table: str, values: Mapping[str, Any]
) -> tuple[tuple[tuple[str, Any], ...], ...]:
    """Every (column, value) tuple that logically identifies this row.

    One entry per partial UNIQUE index whose columns are all non-NULL on
    this row, in :data:`NATURAL_KEYS` order. Empty means the table has no
    natural key beyond its primary key, or that a NULL leaves every index
    inapplicable.

    All of them, not the first (round 2 finding N5): a ``track_locations``
    row carrying both ``file_path`` and ``remote_url`` is subject to both
    indexes at once, and resolving only the first left the second to fail as
    an unrecoverable ``UNIQUE constraint failed`` inside the apply.
    """
    return tuple(
        tuple((column, values[column]) for column in columns)
        for columns in NATURAL_KEYS.get(table, ())
        if all(values.get(column) is not None for column in columns)
    )


def encode_row_pk(values: Sequence[Any]) -> str:
    """``hub_changelog.row_pk`` for a composite primary key.

    A canonical JSON array rather than a delimiter join, so a value that
    itself contains the delimiter cannot forge another row's key.
    """
    return canonical_bytes([None if v is None else str(v) for v in values]).decode("utf-8")


def decode_row_pk(raw: str) -> tuple[str | None, ...]:
    """Inverse of :func:`encode_row_pk`."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SyncProtocolError(f"row_pk {raw!r} is not canonical JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise SyncProtocolError(f"row_pk {raw!r} is not a JSON array")
    return tuple(None if item is None else str(item) for item in parsed)


# ----- schema introspection ---------------------------------------------------


@dataclass
class _ColumnMemo:
    """The columns :func:`table_columns` already read on ``conn`` in one scope."""

    conn: sqlite3.Connection
    columns: dict[str, tuple[str, ...]] = field(default_factory=dict)


_COLUMN_MEMO: ContextVar[_ColumnMemo | None] = ContextVar("table_columns_memo", default=None)


@contextmanager
def table_columns_memo(conn: sqlite3.Connection) -> Iterator[None]:
    """Read each table's columns on ``conn`` once for the length of this scope.

    A per-row walk (an apply batch, a changelog page) asked ``PRAGMA
    table_info`` once per row: 2.5 s of a 10,000-track first sync (LIBM-120
    L6). Scoped rather than a process cache because a connection cannot be
    weakly referenced, so a global key would be ``id(conn)``, which a later
    connection to another database can reuse. The scope holds ``conn`` for
    its whole length, so that cannot happen inside it.

    DDL on a sync-set table inside the scope would not be seen. None runs
    there: those tables change only in migrations, which finish before a
    sync opens the database.
    """
    token = _COLUMN_MEMO.set(_ColumnMemo(conn))
    try:
        yield
    finally:
        _COLUMN_MEMO.reset(token)


def table_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    """Declared columns of ``table``, in declaration order.

    ``table`` must be a known sync-set name; the value is interpolated into
    the PRAGMA (SQLite does not bind identifiers), so the allowlist check is
    load-bearing, not decorative. Inside :func:`table_columns_memo` for the
    same ``conn``, each table is read once.
    """
    if table not in SPEC_BY_TABLE and table not in (MEMBERSHIP_TABLE, REGISTRY_TABLE):
        raise SyncProtocolError(f"{table!r} is not in the sync set")
    memo = _COLUMN_MEMO.get()
    if memo is not None and memo.conn is not conn:
        memo = None
    if memo is not None and table in memo.columns:
        return memo.columns[table]
    columns = _declared_columns(conn, table)
    if memo is not None:
        memo.columns[table] = columns
    return columns


def _declared_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if not rows:
        raise SyncProtocolError(
            f"table {table!r} does not exist; the DB is below migration v6 "
            f"(see specs/design_decision_05.md)"
        )
    return tuple(str(row[1]) for row in rows)


# ----- sort keys ---------------------------------------------------------------


def lww_key(values: Mapping[str, Any], *, table: str | None = None) -> tuple[str, str]:
    """The ``(updated_at, origin_device_id)`` pair ADR 04 c3 compares.

    NULL ``updated_at`` collapses to :data:`EPOCH` / :data:`NO_ORIGIN` so the
    comparison is total. On ``track_fields`` only, a NULL ``updated_at`` with
    a non-NULL ``modified_at`` orders by the normalized fallback instead
    (issue #3101, wire v5); migration v15 backfills ``updated_at`` from
    ``modified_at`` so the fallback is transitional, not permanent.

    The timestamp is normalized first (module docstring point 4): this
    function is called on rows read straight out of a local table as well as
    on rows off the wire, and a locally stored ``2026-08-30T10:00:00Z`` must
    compare EQUAL to the same instant written
    ``2026-08-30T10:00:00.000000+00:00``, not above it.
    """
    updated_at = values.get(UPDATED_AT)
    origin = values.get(ORIGIN_DEVICE_ID)
    if updated_at is None:
        if table == TRACK_FIELDS_TABLE:
            modified_at = values.get(MODIFIED_AT)
            if modified_at is not None:
                return (
                    canonical_timestamp(
                        TRACK_FIELDS_TABLE, MODIFIED_AT, modified_at
                    ),
                    NO_ORIGIN if origin is None else str(origin),
                )
        return (
            EPOCH,
            NO_ORIGIN if origin is None else str(origin),
        )
    return (
        canonical_timestamp("<row>", UPDATED_AT, updated_at),
        NO_ORIGIN if origin is None else str(origin),
    )


def lifecycle_key(values: Mapping[str, Any]) -> str:
    """When a ``tracks`` row last changed between removed and live (wire v7).

    The later of ``deleted_at`` (the user removed it) and ``restored_at`` (the
    user restored it), or :data:`EPOCH` for a row that was never removed. A
    merge compares this BEFORE :func:`lww_key`, so a tombstone beats every
    write that is not a later restore, however new that write's
    ``updated_at`` is: a vendor re-ingest or a stale copy of the library
    carries a newer stamp and an OLDER lifecycle key, and loses.

    Both stamps are normalized for the reason :func:`lww_key` gives; an
    unorderable one raises :class:`SyncProtocolError`.
    """
    stamps = [
        canonical_timestamp(TRACKS_TABLE, column, values[column])
        for column in (DELETED_AT, RESTORED_AT)
        if values.get(column) is not None
    ]
    return max(stamps, default=EPOCH)


def is_hash_pending_candidate(table: str, values: Mapping[str, Any]) -> bool:
    """True when a ``tracks`` row may travel with ``hash_pending: true``.

    Same predicate as the old identity hold (inferred tier, no ``content_hash`` or ``audio_hash``,
    no normalizable ISRC). Used by spoke offer, hub apply validation, and counts.
    """
    if table != "tracks":
        return False
    if str(values.get("stable_id_tier") or "") != "inferred":
        return False
    digest = values.get("content_hash")
    if digest is not None and str(digest).strip():
        return False
    audio_digest = values.get("audio_hash")
    if audio_digest is not None and str(audio_digest).strip():
        return False
    raw_isrc = values.get("isrc")
    return normalise_isrc(None if raw_isrc is None else str(raw_isrc)) is None


def validate_hash_pending_row(table: str, values: Mapping[str, Any], flagged: bool) -> None:
    """Refuse wire rows whose ``hash_pending`` flag disagrees with their values."""
    if not flagged:
        return
    if table != "tracks":
        raise SyncProtocolError(
            f"{table}: hash_pending may only be set on tracks rows, not {table!r}"
        )
    digest = values.get("content_hash")
    if digest is not None and str(digest).strip():
        raise SyncProtocolError(
            f"tracks row {values.get('stable_id')!r}: hash_pending=true "
            f"with a non-empty content_hash is forbidden"
        )
    audio_digest = values.get("audio_hash")
    if audio_digest is not None and str(audio_digest).strip():
        raise SyncProtocolError(
            f"tracks row {values.get('stable_id')!r}: hash_pending=true "
            f"with a non-empty audio_hash is forbidden"
        )
    if not is_hash_pending_candidate(table, values):
        raise SyncProtocolError(
            f"tracks row {values.get('stable_id')!r}: hash_pending=true "
            f"requires inferred tier with no content_hash, no audio_hash, "
            f"and no normalizable ISRC"
        )


__all__ = [
    "DELETED_AT",
    "DIGEST_TABLES",
    "EPOCH",
    "MEMBERSHIP_SPEC",
    "MEMBERSHIP_TABLE",
    "MODIFIED_AT",
    "NATURAL_KEYS",
    "NO_ORIGIN",
    "ORIGIN_DEVICE_ID",
    "REGISTRY_TABLE",
    "RESTORED_AT",
    "SPEC_BY_TABLE",
    "SYNC_COLUMNS",
    "SYNC_TABLES",
    "TRACKS_TABLE",
    "TRACK_FIELDS_TABLE",
    "UPDATED_AT",
    "StampFault",
    "SyncProtocolError",
    "TableSpec",
    "canonical_bytes",
    "canonical_row",
    "canonical_timestamp",
    "canonical_values",
    "decode_row_pk",
    "describe_faults",
    "encode_row_pk",
    "is_hash_pending_candidate",
    "lifecycle_key",
    "lww_key",
    "natural_keys",
    "nfc",
    "pk_columns",
    "stored_stamp_faults",
    "table_columns",
    "table_columns_memo",
    "validate_hash_pending_row",
]
