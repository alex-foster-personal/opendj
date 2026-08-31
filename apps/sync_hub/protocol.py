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
from dataclasses import dataclass
from typing import Any

from apps.sync_hub.protocol_common import (
    DELETED_AT,
    DIGEST_TABLES,
    EPOCH,
    MEMBERSHIP_SPEC,
    MEMBERSHIP_TABLE,
    NATURAL_KEYS,
    NO_ORIGIN,
    ORIGIN_DEVICE_ID,
    REGISTRY_TABLE,
    SPEC_BY_TABLE,
    SYNC_COLUMNS,
    SYNC_TABLES,
    UPDATED_AT,
    SyncProtocolError,
    TableSpec,
    canonical_bytes,
    canonical_row,
    canonical_timestamp,
    canonical_values,
    decode_row_pk,
    encode_row_pk,
    lww_key,
    natural_keys,
    nfc,
    pk_columns,
    table_columns,
)

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
        raw_pk = _require_pk(payload, table, expected=len(SPEC_BY_TABLE[table].pk))
        values = payload.get("values")
        if not isinstance(values, dict):
            raise SyncProtocolError(f"{table}: 'values' must be an object")
        members = _parse_members(payload, table)
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
        raise SyncProtocolError(
            f"{table}: primary key has {expected} column(s), got {len(raw_pk)}"
        )
    return raw_pk


def _parse_members(
    payload: Mapping[str, Any], table: str
) -> tuple[dict[str, Any], ...] | None:
    """``payload['members']``, or ``None`` if absent. Split out of
    :meth:`RowChange.from_wire` for the same reason as :func:`_require_pk`."""
    raw_members = payload.get("members")
    if raw_members is None:
        return None
    if not isinstance(raw_members, list):
        raise SyncProtocolError(f"{table}: 'members' must be an array or absent")
    if table != "playlists":
        raise SyncProtocolError(
            f"{table}: 'members' is only meaningful on a playlists row"
        )
    return tuple(_require_mapping(item, "members[]") for item in raw_members)


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
