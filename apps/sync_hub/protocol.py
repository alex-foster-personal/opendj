"""Wire format, canonical row serialization and per-table digest.

Contract: ``specs/design_decision_04.md``. Everything here is pure -- it
reads a connection to introspect columns and to hash rows, and never
writes. The merge rules live in :mod:`apps.sync_hub.engine`.

Five decisions worth reading before changing anything:

1. **Columns are introspected, never hardcoded.** ``PRAGMA table_info`` is
   the single source of truth so this module cannot drift from
   ``apps.shared.state.schema``. A peer on a different
   :data:`apps.sync_hub.wire_version.WIRE_VERSION` is rejected at the
   handshake rather than silently exchanging half a row; one wire version
   pins one synced column shape, so SCHEMA_VERSION may differ.
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
   wire, in :func:`lww_key`, and in the digest. Canonicalization is
   NORMALIZATION for an orderable value and EXCLUSION for the rest (round
   5): off the wire, unparseable or naive raises :class:`SyncProtocolError`
   and the service turns it into a 422; read out of a LOCAL table, the
   caller asks :func:`stored_stamp_faults` first and quarantines that one
   row -- see :mod:`apps.sync_hub.protocol_common`'s docstring for the
   direction split.
5. **Strings are NFC-normalized before hashing** (ADR 08 point 6a, round 1
   finding 6a). macOS hands back NFD paths, Windows and Linux NFC; both are
   correct spellings of one string and neither the digest nor a UNIQUE index
   saw them as equal. Primary key columns are deliberately EXCLUDED from
   that normalization: a pk is an identity, not text, and silently re-spelling
   one would make two peers disagree about which row they are talking about.
   Every pk in the sync set is machine-generated ASCII (sha1 hex, uuid hex,
   or a CHECK-constrained enum), so there is nothing there to normalize.

Split into :mod:`apps.sync_hub.protocol_common` (quality-gate file_size
ratchet, round 4): the table specs and the canonicalization/introspection/
sort-key rules above moved there, dependency-free of the rest of this
package. This module keeps the wire payload dataclasses (``RowChange``,
``MachineRow``, ``SyncDigest``), the digest functions, and the small
``from_wire`` parsing helpers, and re-exports everything from
``protocol_common`` so every caller that already does
``from apps.sync_hub import protocol`` and reads ``protocol.X`` keeps the
surface it had.
"""
from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from apps.sync_hub import digest_gate, sync_set
from apps.sync_hub.machine_wire_limits import (
    MachineWireLimitError,
    validate_machine_wire_fields,
)
from apps.sync_hub.protocol_common import (
    DELETED_AT,
    DIGEST_TABLES,
    EPOCH,
    MEMBERSHIP_SPEC,
    MEMBERSHIP_TABLE,
    MODIFIED_AT,
    NATURAL_KEYS,
    NO_ORIGIN,
    ORIGIN_DEVICE_ID,
    REGISTRY_TABLE,
    SPEC_BY_TABLE,
    SYNC_COLUMNS,
    SYNC_TABLES,
    TRACK_FIELDS_TABLE,
    UPDATED_AT,
    StampFault,
    SyncProtocolError,
    TableSpec,
    canonical_bytes,
    canonical_row,
    canonical_timestamp,
    canonical_values,
    decode_row_pk,
    describe_faults,
    encode_row_pk,
    lww_key,
    natural_keys,
    nfc,
    pk_columns,
    stored_stamp_faults,
    table_columns,
    table_columns_memo,
    validate_hash_pending_row,
)
from apps.sync_hub.quarantine_log import quarantine_pass, record_quarantine

# ----- wire payloads -----------------------------------------------------


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
    hash_pending: bool = False

    @property
    def row_pk(self) -> str:
        return encode_row_pk(self.pk)

    @property
    def updated_at(self) -> str:
        return lww_key(self.values, table=self.table)[0]

    @property
    def origin_device_id(self) -> str:
        return lww_key(self.values, table=self.table)[1]

    @property
    def sort_key(self) -> tuple[str, str]:
        return lww_key(self.values, table=self.table)

    def to_wire(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "table": self.table,
            "pk": list(self.pk),
            "values": self.values,
        }
        if self.members is not None:
            payload["members"] = [dict(member) for member in self.members]
        if self.hash_pending:
            payload["hash_pending"] = True
        return payload

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> RowChange:
        table = _require_str(payload, "table")
        if table not in SPEC_BY_TABLE:
            raise SyncProtocolError(
                f"{table!r} is not a pushable table; the sync set is "
                f"{sorted(SPEC_BY_TABLE)}"
            )
        raw_pk = _require_pk(payload, table, expected=len(SPEC_BY_TABLE[table].pk))
        values = payload.get("values")
        if not isinstance(values, dict):
            raise SyncProtocolError(f"{table}: 'values' must be an object")
        members = _parse_members(payload, table)
        raw_hp = payload.get("hash_pending", False)
        if raw_hp not in (False, True, None):
            raise SyncProtocolError(
                f"{table}: 'hash_pending' must be a boolean or absent, got {raw_hp!r}"
            )
        hash_pending = bool(raw_hp)
        # Canonicalize BEFORE the row can reach a comparison or a table:
        # what this peer sent is the last chance to reject a timestamp that
        # cannot be ordered (module docstring point 4).
        validate_hash_pending_row(table, values, hash_pending)
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
            hash_pending=hash_pending,
        )


@dataclass(frozen=True)
class IdentityReject:
    """One identity-collapse rejection on push: the offered PK lost to a stored survivor."""

    table: str
    offered_pk: str
    survivor_pk: str

    def to_wire(self) -> dict[str, str]:
        return {
            "table": self.table,
            "offered_pk": self.offered_pk,
            "survivor_pk": self.survivor_pk,
        }

    @classmethod
    def from_wire(cls, payload: Mapping[str, Any]) -> IdentityReject:
        table = _require_str(payload, "table")
        return cls(
            table=table,
            offered_pk=_require_str(payload, "offered_pk"),
            survivor_pk=_require_str(payload, "survivor_pk"),
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
        machine_id = _require_str(payload, "machine_id")
        name = _require_str(payload, "name")
        try:
            validate_machine_wire_fields(machine_id, name)
        except MachineWireLimitError as exc:
            raise SyncProtocolError(str(exc)) from exc
        return cls(
            machine_id=machine_id,
            name=name,
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
    #: Rows per table this peer EXCLUDED from the hash because a stored stamp
    #: is unorderable (round 5). Not folded into ``overall``: the two sides
    #: are comparing the rows that are actually eligible to sync, and two
    #: peers with identical eligible content and different quarantine counts
    #: have converged. ``None`` means the peer DID NOT REPORT -- an older
    #: build, or a payload without the field -- and must be surfaced as
    #: unknown, never coerced to 0, or the readout becomes a failed
    #: measurement rendered as a clean result.
    quarantined: dict[str, int] | None = field(default=None)
    #: Rows per table offered or held as hash_pending (ADR-0068). Included IN
    #: the hash but counted separately from ``quarantined``. ``None`` when the
    #: peer did not report the field.
    hash_pending: dict[str, int] | None = field(default=None)
    #: Of ``quarantined``, rows the hub already settled and this machine does not hold (CLOUDSYNC-32). Local only.
    settled: dict[str, int] | None = field(default=None)

    @property
    def hash_pending_rows(self) -> int | None:
        """Total rows this peer reported as hash_pending, or None when unknown."""
        return None if self.hash_pending is None else sum(self.hash_pending.values())

    @property
    def quarantined_rows(self) -> int | None:
        """Total rows this peer quarantined, or None when it did not report."""
        return None if self.quarantined is None else sum(self.quarantined.values())

    def to_wire(self) -> dict[str, Any]:
        return {
            "tables": dict(self.tables),
            "overall": self.overall,
            "seq": self.seq,
            "quarantined": None if self.quarantined is None else dict(self.quarantined),
            "hash_pending": None if self.hash_pending is None else dict(self.hash_pending),
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
        raw_quarantined = payload.get("quarantined")
        if raw_quarantined is not None and not isinstance(raw_quarantined, dict):
            raise SyncProtocolError(
                f"digest payload 'quarantined' must be an object or absent, "
                f"got {raw_quarantined!r}"
            )
        raw_hash_pending = payload.get("hash_pending")
        if raw_hash_pending is not None and not isinstance(raw_hash_pending, dict):
            raise SyncProtocolError(
                f"digest payload 'hash_pending' must be an object or absent, "
                f"got {raw_hash_pending!r}"
            )
        return cls(
            tables={str(k): str(v) for k, v in tables.items()},
            overall=_require_str(payload, "overall"),
            seq=seq,
            quarantined=(
                None
                if raw_quarantined is None
                else {str(k): int(v) for k, v in raw_quarantined.items()}
            ),
            hash_pending=(
                None
                if raw_hash_pending is None
                else {str(k): int(v) for k, v in raw_hash_pending.items()}
            ),
        )

    def divergent_tables(self, other: SyncDigest) -> tuple[str, ...]:
        """Table names whose digests differ (or exist on only one side)."""
        names = sorted(set(self.tables) | set(other.tables))
        return tuple(
            name for name in names if self.tables.get(name) != other.tables.get(name)
        )

    def inconclusive_tables(self, other: SyncDigest) -> tuple[str, ...]:
        """Divergent tables whose comparison EXCLUDED rows on one side.

        A table where either peer held rows out of its sync set did not
        compare two complete sets, so a difference there is UNMEASURED, not
        divergence. It happens the moment a row that already reached the peer
        is later quarantined locally: the peer still hashes it, this machine
        no longer can, and without this the ADR 04 c6 corruption alarm would
        fire on every sync forever -- quarantine as the brick under a new
        name, which is the failure mode this whole round exists to remove.

        Empty when EITHER side did not report a quarantine map: "did not
        report" is not "reported zero", and a mismatch explained by a
        measurement nobody took is not explained at all.
        """
        if self.quarantined is None or other.quarantined is None:
            return ()
        return tuple(
            name
            for name in self.divergent_tables(other)
            if self.quarantined.get(name, 0) or other.quarantined.get(name, 0)
        )


# ----- digest --------------------------------------------------------------


@dataclass(frozen=True)
class TableDigest:
    """One table's hash and how many of its rows never entered it."""

    hash: str
    quarantined: int
    hash_pending: int = 0
    settled: int = 0  #: Of ``quarantined``, rows the hub already settled (CLOUDSYNC-32).


def table_digest(
    conn: sqlite3.Connection, table: str, held: sync_set.HeldKeys | None = None
) -> TableDigest:
    """sha256 over the ordered SYNC-ELIGIBLE canonical rows of ``table``.

    Tombstones included: a peer that dropped a ``deleted_at`` row has
    diverged, and this is the only check that would notice.

    Rows are hashed through :func:`canonical_row`, so two peers holding the
    same instant spelled differently, or the same path in NFD and NFC, agree
    (module docstring points 4 and 5). That is not a weakening of the check:
    those rows ARE the same content, and before ADR 08 the difference halted
    a machine that had done nothing wrong.

    A row that is not in the sync set (:mod:`apps.sync_hub.sync_set`) is
    EXCLUDED and counted (round 5) rather than raising. Hashing its raw bytes
    would be worse than either: the row never reaches a peer, so the two
    digests would differ forever and ``run_sync`` would raise
    :class:`SyncDigestMismatch` -- the one alarm ADR 04 c6 reserves for a
    merge bug -- on every sync of a library holding one legacy row. Excluding
    it means the digest answers the question it is asked (did the rows that
    CAN sync converge?) and the count beside it says the rest never left.

    ``held`` carries the exclusions from the tables already walked, and is
    the reason this must be driven from :func:`sync_digest` rather than
    called per table: a standalone call sees no parent held in another table,
    so it can hash a child the push cannot offer. It defaults to an empty set
    for the per-table callers that only compare one table against itself.

    Callers that compare two digests MUST hold a read transaction open
    across the whole comparison (ADR 08 point 6b): without one, a table read
    late in :func:`sync_digest` can reflect a writer that landed after an
    earlier table was read, and the rollup describes a state that never
    existed.
    """
    spec = sync_set.spec_for(table)
    tracking = sync_set.HeldKeys(conn) if held is None else held
    columns = table_columns(conn, table)
    order_by = ", ".join(spec.pk)
    digest = hashlib.sha256()
    digest.update(canonical_bytes({"table": table, "columns": list(columns)}))
    quarantined = settled = 0
    hash_pending = 0
    cursor = conn.execute(
        f"SELECT {', '.join(columns)} FROM {table} ORDER BY {order_by}"
    )
    for row in cursor:
        reason = sync_set.excluded_reason(conn, table, columns, row, spec, tracking)
        if reason is not None:
            quarantined += 1
            if sync_set.is_settled(reason):
                settled += 1
                continue
            pk_index = {column: index for index, column in enumerate(columns)}
            pk = [row[pk_index[column]] for column in spec.pk]
            record_quarantine(table, pk, reason)
            continue
        canonical = canonical_row(table, columns, row)
        if table == "tracks" and sync_set.is_hash_pending_track(
            dict(zip(columns, row, strict=True)), held=tracking
        ):
            hash_pending += 1
        digest.update(canonical_bytes(canonical))
    return TableDigest(digest.hexdigest(), quarantined, hash_pending, settled)


def sync_digest(conn: sqlite3.Connection, *, seq: int = 0) -> SyncDigest:
    """:func:`compute_sync_digest`, reused while nothing it reads has changed.

    Issue #4396: a no-op sync used to re-walk every row of every table on
    both machines. :func:`apps.sync_hub.digest_gate.gated_digest` returns
    the previous answer only when the changelog seqs, the schema, the
    identity remap and every table's trigger-maintained write token prove it
    current, and walks in full whenever they cannot. Callers keep the same
    contract, including holding one transaction across the call.
    """
    return digest_gate.gated_digest(
        conn, seq, lambda: compute_sync_digest(conn, seq=seq)
    )


def compute_sync_digest(conn: sqlite3.Connection, *, seq: int = 0) -> SyncDigest:
    """Digest every table in :data:`DIGEST_TABLES`, plus a rollup. Always walks.

    ``seq`` is carried through untouched: the caller reads it from the
    changelog inside the same transaction as this call, so the digest and
    the position it describes are one snapshot.

    Walks :data:`apps.sync_hub.sync_set.FK_ORDER`, not ``DIGEST_TABLES``:
    exclusions are transitive over the foreign keys, so a parent must be
    judged before its children. The returned mapping is the same set of
    tables either way, and the rollup sorts its keys, so the ORDER of the
    walk changes nothing about the answer -- only about its correctness.

    The rollup hashes the per-table HASHES only, never the quarantine
    counts: two peers holding identical eligible content converge even when
    one of them is holding a legacy row back.
    """
    with quarantine_pass("digest"):
        held = sync_set.HeldKeys(conn)
        computed = {
            name: table_digest(conn, name, held) for name in sync_set.FK_ORDER
        }
        tables = {name: value.hash for name, value in computed.items()}
        overall = hashlib.sha256(canonical_bytes(tables)).hexdigest()
        return SyncDigest(
            tables=tables,
            overall=overall,
            seq=seq,
            quarantined={
                name: value.quarantined
                for name, value in computed.items()
                if value.quarantined
            },
            hash_pending={
                name: value.hash_pending
                for name, value in computed.items()
                if value.hash_pending
            },
            settled={name: value.settled for name, value in computed.items() if value.settled},
        )


# ----- small parsing helpers ------------------------------------------------


def _require_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise SyncProtocolError(f"payload field {key!r} must be a non-empty string")
    return value


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SyncProtocolError(f"{label} must be an object, got {type(value).__name__}")
    return dict(value)


def _require_pk(payload: Mapping[str, Any], table: str, *, expected: int) -> list[Any]:
    """``payload['pk']``, checked shape and arity. Split out of
    :meth:`RowChange.from_wire` to keep its own branch count under the
    quality-gate complexity limit (mccabe 12)."""
    raw_pk = payload.get("pk")
    if not isinstance(raw_pk, list) or not raw_pk:
        raise SyncProtocolError(f"{table}: 'pk' must be a non-empty array")
    if len(raw_pk) != expected:
        raise SyncProtocolError(f"{table}: primary key has {expected} column(s), got {len(raw_pk)}")
    return raw_pk


def _parse_members(payload: Mapping[str, Any], table: str) -> tuple[dict[str, Any], ...] | None:
    """``payload['members']``, or ``None`` if absent. Split out of
    :meth:`RowChange.from_wire` for the same reason as :func:`_require_pk`."""
    raw_members = payload.get("members")
    if raw_members is None:
        return None
    if not isinstance(raw_members, list):
        raise SyncProtocolError(f"{table}: 'members' must be an array or absent")
    if table != "playlists":
        raise SyncProtocolError(f"{table}: 'members' is only meaningful on a playlists row")
    return tuple(_require_mapping(item, "members[]") for item in raw_members)


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
    "SPEC_BY_TABLE",
    "SYNC_COLUMNS",
    "SYNC_TABLES",
    "TRACK_FIELDS_TABLE",
    "UPDATED_AT",
    "IdentityReject",
    "MachineRow",
    "RowChange",
    "StampFault",
    "SyncDigest",
    "SyncProtocolError",
    "TableDigest",
    "TableSpec",
    "canonical_bytes",
    "canonical_row",
    "canonical_timestamp",
    "canonical_values",
    "compute_sync_digest",
    "decode_row_pk",
    "describe_faults",
    "encode_row_pk",
    "lww_key",
    "natural_keys",
    "nfc",
    "pk_columns",
    "stored_stamp_faults",
    "sync_digest",
    "table_columns",
    "table_columns_memo",
    "table_digest",
]
