"""Wire format, canonical row serialization and per-table digest.

Contract: ``specs/design_decision_04.md``. Everything here is pure -- it
reads a connection to introspect columns and to hash rows, and never
writes. The merge rules live in :mod:`apps.sync_hub.engine`.

Five decisions worth reading before changing anything:

1. **Columns are introspected, never hardcoded.** ``PRAGMA table_info`` is
   the single source of truth so this module cannot drift from
   ``apps.shared.state.schema``. A peer on a different ``SCHEMA_VERSION`` is
   rejected at the handshake rather than silently exchanging half a row.
2. **NULL ``updated_at`` sorts as epoch** (ADR 04 c7): legacy rows written
   before migration v6 always lose to a real edit. The sentinel is a real
   ISO string (:data:`EPOCH`) rather than ``None`` so every comparison is a
   plain lexicographic tuple compare with no branch.
3. **``machines`` is a registry, not an LWW table.** ADR 04 c8 lists it in
   the sync set, but the DDL in ADR 05 gives it no
   ``updated_at`` / ``origin_device_id`` / ``deleted_at``, so it cannot take
   part in per-row LWW, and its ``last_seen`` heartbeat is *expected* to
   differ between peers. It therefore rides the pull response as a snapshot
   (so the FKs from ``sync_policies`` / ``playlist_pins`` resolve) and is
   excluded from the digest. Recorded as an ADR gap, not papered over.
4. **Every ``updated_at`` crossing this boundary is normalized** (ADR 08
   point 2, round 1 finding 2). Plain string comparison of ISO8601 is not an
   ordering over instants -- a ``Z`` suffix outsorts an identical
   ``+00:00`` instant, microsecond precision outsorts second precision, and
   a ``+01:00`` offset makes an EARLIER instant sort LATER. So the value is
   parsed with :func:`apps.shared.state.sync_stamp.parse_canonical` and
   re-emitted in the one canonical format everywhere it is read: on the
   wire, in :func:`lww_key`, and in the digest. Anything unparseable or
   naive raises :class:`SyncProtocolError`, which the service turns into a
   422 rather than storing a row nothing can order.
5. **Strings are NFC-normalized before hashing** (ADR 08 point 6a, round 1
   finding 6a). macOS hands back NFD paths, Windows and Linux NFC; both are
   correct spellings of one string and neither the digest nor a UNIQUE index
   saw them as equal. Primary key columns are deliberately EXCLUDED from
   that normalization: a pk is an identity, not text, and silently re-spelling
   one would make two peers disagree about which row they are talking about.
   Every pk in the sync set is machine-generated ASCII (sha1 hex, uuid hex,
   or a CHECK-constrained enum), so there is nothing there to normalize.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from apps.shared.state import sync_stamp

# A row whose ``updated_at`` is NULL predates migration v6. ADR 04 c7 says it
# syncs as epoch-old. This literal sorts before any real ISO8601 timestamp
# under plain string comparison, which is the only ordering the protocol uses.
EPOCH: str = "0000-01-01T00:00:00+00:00"

# Same idea for a NULL origin: it must never win the tiebreak.
NO_ORIGIN: str = ""


class SyncProtocolError(ValueError):
    """A payload could not be read as this protocol. Never swallowed."""


# ----- table specs ---------------------------------------------------------


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
    TableSpec("playlists", ("playlist_id",)),
    TableSpec("track_vendor_ids", ("stable_id", "vendor")),
    TableSpec("track_fields", ("stable_id", "field_name")),
    TableSpec("track_locations", ("location_id",)),
    TableSpec("sync_policies", ("machine_id", "asset_kind")),
    TableSpec("playlist_pins", ("machine_id", "playlist_id")),
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

# Fleet registry. See point 3 in the module docstring.
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
SYNC_COLUMNS: tuple[str, ...] = (UPDATED_AT, ORIGIN_DEVICE_ID, DELETED_AT)


# ----- canonical serialization --------------------------------------------


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
    if column in (UPDATED_AT, DELETED_AT):
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


# ----- schema introspection ------------------------------------------------


def table_columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    """Declared columns of ``table``, in declaration order.

    ``table`` must be a known sync-set name; the value is interpolated into
    the PRAGMA (SQLite does not bind identifiers), so the allowlist check is
    load-bearing, not decorative.
    """
    if table not in SPEC_BY_TABLE and table not in (MEMBERSHIP_TABLE, REGISTRY_TABLE):
        raise SyncProtocolError(f"{table!r} is not in the sync set")
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if not rows:
        raise SyncProtocolError(
            f"table {table!r} does not exist; the DB is below migration v6 "
            f"(see specs/design_decision_05.md)"
        )
    return tuple(str(row[1]) for row in rows)


# ----- sort keys -----------------------------------------------------------


def lww_key(values: Mapping[str, Any]) -> tuple[str, str]:
    """The ``(updated_at, origin_device_id)`` pair ADR 04 c3 compares.

    NULLs collapse to :data:`EPOCH` / :data:`NO_ORIGIN` so the comparison is
    total and a legacy row always loses. The timestamp is normalized first
    (module docstring point 4): this function is called on rows read straight
    out of a local table as well as on rows off the wire, and a locally
    stored ``2026-08-30T10:00:00Z`` must compare EQUAL to the same instant
    written ``2026-08-30T10:00:00.000000+00:00``, not above it.
    """
    updated_at = values.get(UPDATED_AT)
    origin = values.get(ORIGIN_DEVICE_ID)
    return (
        EPOCH
        if updated_at is None
        else canonical_timestamp("<row>", UPDATED_AT, updated_at),
        NO_ORIGIN if origin is None else str(origin),
    )


# ----- wire payloads -------------------------------------------------------


@dataclass(frozen=True)
class RowChange:
    """One row offered to a peer, with its whole-playlist membership bundle.

    ``members`` is non-None only for ``playlists`` rows and then carries the
    complete membership list for that playlist (ADR 04 c5), tombstones
    included. An empty tuple means "this playlist has no tracks", which is
    different from ``None`` ("this is not a playlist row").
    """

    table: str
    pk: tuple[str, ...]
    values: dict[str, Any]
    members: tuple[dict[str, Any], ...] | None = None

    @property
    def row_pk(self) -> str:
        return encode_row_pk(self.pk)

    @property
    def updated_at(self) -> str:
        return lww_key(self.values)[0]

    @property
    def origin_device_id(self) -> str:
        return lww_key(self.values)[1]

    @property
    def sort_key(self) -> tuple[str, str]:
        return lww_key(self.values)

    def to_wire(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "table": self.table,
            "pk": list(self.pk),
            "values": self.values,
        }
        if self.members is not None:
            payload["members"] = [dict(member) for member in self.members]
        return payload

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> RowChange:
        table = _require_str(payload, "table")
        if table not in SPEC_BY_TABLE:
            raise SyncProtocolError(
                f"{table!r} is not a pushable table; the sync set is "
                f"{sorted(SPEC_BY_TABLE)}"
            )
        raw_pk = payload.get("pk")
        if not isinstance(raw_pk, list) or not raw_pk:
            raise SyncProtocolError(f"{table}: 'pk' must be a non-empty array")
        expected = len(SPEC_BY_TABLE[table].pk)
        if len(raw_pk) != expected:
            raise SyncProtocolError(
                f"{table}: primary key has {expected} column(s), got {len(raw_pk)}"
            )
        values = payload.get("values")
        if not isinstance(values, dict):
            raise SyncProtocolError(f"{table}: 'values' must be an object")
        raw_members = payload.get("members")
        members: tuple[dict[str, Any], ...] | None
        if raw_members is None:
            members = None
        elif isinstance(raw_members, list):
            if table != "playlists":
                raise SyncProtocolError(
                    f"{table}: 'members' is only meaningful on a playlists row"
                )
            members = tuple(_require_mapping(item, "members[]") for item in raw_members)
        else:
            raise SyncProtocolError(f"{table}: 'members' must be an array or absent")
        # Canonicalize BEFORE the row can reach a comparison or a table:
        # what this peer sent is the last chance to reject a timestamp that
        # cannot be ordered (module docstring point 4).
        return cls(
            table=table,
            pk=tuple(str(item) for item in raw_pk),
            values=canonical_values(table, values),
            members=(
                None
                if members is None
                else tuple(
                    canonical_values(MEMBERSHIP_TABLE, member) for member in members
                )
            ),
        )


@dataclass(frozen=True)
class MachineRow:
    """One ``machines`` row on the wire (registry, not LWW)."""

    machine_id: str
    name: str
    platform: str
    is_hub: bool
    data_root: str | None
    first_seen: str
    last_seen: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "machine_id": self.machine_id,
            "name": self.name,
            "platform": self.platform,
            "is_hub": self.is_hub,
            "data_root": self.data_root,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
        }

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> MachineRow:
        raw_root = payload.get("data_root")
        return cls(
            machine_id=_require_str(payload, "machine_id"),
            name=_require_str(payload, "name"),
            platform=_require_str(payload, "platform"),
            is_hub=bool(payload.get("is_hub", False)),
            data_root=None if raw_root is None else str(raw_root),
            first_seen=_require_str(payload, "first_seen"),
            last_seen=_require_str(payload, "last_seen"),
        )


@dataclass(frozen=True)
class SyncDigest:
    """Per-table digests plus one rollup, for the post-sync comparison.

    ``seq`` is the changelog position the digest describes, read in the same
    transaction as the hashes (round 2 finding 6b). A spoke comparing its
    own state against a hub digest taken at a HIGHER seq than the spoke
    pulled to is not looking at divergence -- it is looking at a third
    machine's push that landed in the gap. Zero on a digest computed
    locally, where there is no changelog to fence against.
    """

    tables: dict[str, str]
    overall: str
    seq: int = 0

    def to_wire(self) -> dict[str, Any]:
        return {
            "tables": dict(self.tables),
            "overall": self.overall,
            "seq": self.seq,
        }

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> SyncDigest:
        tables = payload.get("tables")
        if not isinstance(tables, dict):
            raise SyncProtocolError("digest payload lacks a 'tables' object")
        seq = payload.get("seq", 0)
        if not isinstance(seq, int) or isinstance(seq, bool):
            raise SyncProtocolError(
                f"digest payload 'seq' must be an integer, got {seq!r}"
            )
        return cls(
            tables={str(k): str(v) for k, v in tables.items()},
            overall=_require_str(payload, "overall"),
            seq=seq,
        )

    def divergent_tables(self, other: SyncDigest) -> tuple[str, ...]:
        """Table names whose digests differ (or exist on only one side)."""
        names = sorted(set(self.tables) | set(other.tables))
        return tuple(
            name for name in names if self.tables.get(name) != other.tables.get(name)
        )


# ----- digest --------------------------------------------------------------


def table_digest(conn: sqlite3.Connection, table: str) -> str:
    """sha256 over the ordered canonical rows of ``table``.

    Tombstones included: a peer that dropped a ``deleted_at`` row has
    diverged, and this is the only check that would notice.

    Rows are hashed through :func:`canonical_row`, so two peers holding the
    same instant spelled differently, or the same path in NFD and NFC, agree
    (module docstring points 4 and 5). That is not a weakening of the check:
    those rows ARE the same content, and before ADR 08 the difference halted
    a machine that had done nothing wrong. A timestamp that cannot be parsed
    at all still raises rather than hashing.

    Callers that compare two digests MUST hold a read transaction open
    across the whole comparison (ADR 08 point 6b): without one, a table read
    late in :func:`sync_digest` can reflect a writer that landed after an
    earlier table was read, and the rollup describes a state that never
    existed.
    """
    spec = SPEC_BY_TABLE.get(table)
    if spec is None and table == MEMBERSHIP_TABLE:
        spec = MEMBERSHIP_SPEC
    if spec is None:
        raise SyncProtocolError(f"{table!r} is not in the digest set")
    columns = table_columns(conn, table)
    order_by = ", ".join(spec.pk)
    digest = hashlib.sha256()
    digest.update(canonical_bytes({"table": table, "columns": list(columns)}))
    cursor = conn.execute(
        f"SELECT {', '.join(columns)} FROM {table} ORDER BY {order_by}"
    )
    for row in cursor:
        digest.update(canonical_bytes(canonical_row(table, columns, row)))
    return digest.hexdigest()


def sync_digest(conn: sqlite3.Connection, *, seq: int = 0) -> SyncDigest:
    """Digest every table in :data:`DIGEST_TABLES`, plus a rollup.

    ``seq`` is carried through untouched: the caller reads it from the
    changelog inside the same transaction as this call, so the digest and
    the position it describes are one snapshot.
    """
    tables = {name: table_digest(conn, name) for name in DIGEST_TABLES}
    overall = hashlib.sha256(canonical_bytes(tables)).hexdigest()
    return SyncDigest(tables=tables, overall=overall, seq=seq)


# ----- small parsing helpers ----------------------------------------------


def _require_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise SyncProtocolError(f"payload field {key!r} must be a non-empty string")
    return value


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SyncProtocolError(f"{label} must be an object, got {type(value).__name__}")
    return dict(value)


__all__ = [
    "DELETED_AT",
    "DIGEST_TABLES",
    "EPOCH",
    "MEMBERSHIP_SPEC",
    "MEMBERSHIP_TABLE",
    "NATURAL_KEYS",
    "NO_ORIGIN",
    "ORIGIN_DEVICE_ID",
    "REGISTRY_TABLE",
    "SPEC_BY_TABLE",
    "SYNC_COLUMNS",
    "SYNC_TABLES",
    "UPDATED_AT",
    "MachineRow",
    "RowChange",
    "SyncDigest",
    "SyncProtocolError",
    "TableSpec",
    "canonical_bytes",
    "canonical_row",
    "canonical_timestamp",
    "canonical_values",
    "decode_row_pk",
    "encode_row_pk",
    "lww_key",
    "natural_keys",
    "nfc",
    "pk_columns",
    "sync_digest",
    "table_columns",
    "table_digest",
]
