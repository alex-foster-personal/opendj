"""``WIRE_VERSION``: what a hub and a spoke must AGREE on, split from storage.

ADR-0012 section D named the defect and deferred the fix: the handshake used
to refuse any peer whose ``schema_version`` was not EQUAL to its own, so every
shared-ladder step (v9 enrollment, v10 karaoke, whatever comes next) forced
every machine in the fleet to upgrade in one sitting or get a 409 on every
call -- even when no synced row changed shape. Two different versions were
being read as one:

* ``apps.shared.state.schema.SCHEMA_VERSION`` is LOCAL STORAGE. It says how
  far this machine's migration ladder has run, and it moves on every step,
  including steps that only touch machine-local tables.
* :data:`WIRE_VERSION` is the PROTOCOL. It names the shape of every row the
  two sides exchange, and it moves only when that shape does.

The gate (:func:`incompatibility`) compares WIRE versions. Two peers on
different schema versions but the same wire version sync, because the same
wire version GUARANTEES the same synced row shape on both sides: that is what
:data:`WIRE_FINGERPRINTS` pins, and ``tests/cloudsync/test_wire_shape_guard``
plus drift-lint rule D-12 fail the build when the shape moves without a bump.
Exact equality, deliberately, not a supported RANGE: accepting an older wire
would mean translating its payloads, and no code does that. A range with no
translator behind it would be a claim with nothing measuring it.

**WHAT IS A WIRE CHANGE.** Bump :data:`WIRE_VERSION` by one and record the new
fingerprint in :data:`WIRE_FINGERPRINTS` IN THE SAME COMMIT when you:

1. add, remove, rename or reorder a column of any table in the digest set
   (``SYNC_TABLES`` plus ``playlist_memberships``);
2. change a declared type, ``NOT NULL``, primary-key membership, foreign key,
   ``CHECK`` or ``UNIQUE`` constraint on one of those tables -- a CHECK widened
   on one side (v10's ``sync_policies.asset_kind``) is a row the other side
   refuses mid-push;
3. add a table to, or remove one from, the sync set;
4. change the fields of :class:`apps.sync_hub.protocol.MachineRow`;
5. change what an existing field MEANS without changing its shape (a new
   canonical stamp format, say). The fingerprint cannot see this one, so it is
   the one bump that must be made by hand.

**NOT a wire change:** a ladder step that touches only machine-local tables; a
non-unique index; a column ``DEFAULT`` (every column crosses the wire
explicitly and :mod:`apps.sync_hub.engine_apply` refuses a row whose column
set differs, so a default never supplies a synced value); and an OPTIONAL
request or response field older peers ignore, which is what per-request
capability tokens (:mod:`apps.sync_hub.capabilities`, ``quarantine/v1``)
negotiate.

**A peer that sends no ``wire_version``** is a build from before this split.
Absent is never read as "same wire" (the rule
:func:`apps.sync_hub.capabilities.understands_quarantine` states for tokens):
such a peer is judged by the rule it applies itself, exact schema equality,
which is still sound because one schema version is one row shape -- D-07
fingerprints every shipped ladder step so that stays true. That keeps both
rollout orders working: a pre-split spoke against this hub, and this spoke
against a pre-split hub, sync exactly when they did before.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import sqlite3
from typing import Any, Literal

from apps.shared.state import schema as state_schema
from apps.sync_hub import protocol

#: The sync protocol's version. See the module docstring for what bumps it.
#: ``PushResponse.identity_rejects`` (issue #3057) is an OPTIONAL response
#: field older peers ignore, advertised by the ``identity-reject/v1``
#: capability token: NOT a wire change under the rule above, so no bump.
WIRE_VERSION: int = 6

#: Row-shape fingerprint of every wire version that has shipped, oldest
#: first. APPEND-ONLY: an entry is a fact about deployed peers, and rewriting
#: one in place would let two builds claim the same wire with different rows.
#: Keys must run 1..WIRE_VERSION with no gaps and no repeated value.
WIRE_FINGERPRINTS: dict[int, str] = {
    # v1: the shape at shared-state schema v10 (lyric_verdict, sync_policies
    # with the six-kind asset_kind CHECK). Measured Fri 11 Sep 2026.
    1: "a0edfa096f9682a2",
    # v2: feedback_pins (FBSYNC-01) joins the digest set. Measured Fri 12 Sep 2026.
    2: "46eb07a34e02e019",
    # v3: playlist_memberships item_id + order_key (LIBM-20). Measured Sat 12 Sep 2026.
    3: "00a3594073b7dc9b",
    # v4: playlists forbid_duplicates (LIBM-D2 / #2416). Measured Sun 13 Sep 2026.
    4: "6985ab3a573661be",
    # v5: track_fields LWW falls back to modified_at when updated_at is NULL
    # (issue #3101). Row shape unchanged; semantic contract marker below.
    5: "8945d178ba66d099",
    # v6: tracks.audio_hash (tag-independent audio identity, issue #3864).
    6: "087fd8afdbb6c91b",
}

#: Semantic contract ids keyed by wire version. Row-shape fingerprints cannot
#: see a meaning change, so a wire bump that changes LWW ordering without
#: altering columns records its contract here and folds it into
#: :func:`measure_wire_shape`.
WIRE_SEMANTIC_CONTRACTS: dict[int, str] = {
    5: "track_fields_modified_at_lww_fallback",
    6: "track_fields_modified_at_lww_fallback",
}

#: 409 codes the gate answers with. SYNC_SCHEMA_VERSION is kept verbatim for
#: the pre-split rule, so an operator's existing runbook still matches it.
CODE_WIRE: str = "SYNC_WIRE_VERSION"
CODE_SCHEMA: str = "SYNC_SCHEMA_VERSION"


class WireShapeError(RuntimeError):
    """The wire shape could not be MEASURED. Never read as a verdict."""


@dataclasses.dataclass(frozen=True)
class Incompatibility:
    """Why two peers must not exchange rows. ``code`` is the 409 code."""

    code: str
    message: str


@dataclasses.dataclass(frozen=True)
class UpdateRequiredState:
    """Typed wire-version mismatch surfaced to operators and the UI."""

    code: Literal["SYNC_WIRE_VERSION"]
    local_wire_version: int
    peer_wire_version: int
    action: str


_UPDATE_REQUIRED_ACTION: str = "install the latest Open DJ"
_LOCAL_WIRE_RE = re.compile(r"this machine speaks v(\d+)", re.IGNORECASE)
_PEER_WIRE_RE = re.compile(
    r"(?:peer|hub) speaks sync wire v(\d+)", re.IGNORECASE
)


def parse_update_required(error_message: str) -> UpdateRequiredState | None:
    """Return a typed update-required state when ``error_message`` is wire mismatch."""
    if not error_message.strip():
        return None
    code: str | None = None
    json_start = error_message.find("{")
    if json_start >= 0:
        tail = error_message[json_start:]
        try:
            payload = json.loads(tail)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            detail = payload.get("detail")
            if isinstance(detail, dict) and isinstance(detail.get("code"), str):
                code = detail["code"]
    if code is None and CODE_WIRE in error_message:
        code = CODE_WIRE
    if code != CODE_WIRE and "SyncVersionMismatch" not in error_message:
        return None
    local_match = _LOCAL_WIRE_RE.search(error_message)
    peer_match = _PEER_WIRE_RE.search(error_message)
    if local_match is None or peer_match is None:
        return None
    return UpdateRequiredState(
        code=CODE_WIRE,
        local_wire_version=int(local_match.group(1)),
        peer_wire_version=int(peer_match.group(1)),
        action=_UPDATE_REQUIRED_ACTION,
    )


# ----- the gate ----------------------------------------------------------------


def incompatibility(
    peer_wire: int | None, peer_schema: int, *, peer: str
) -> Incompatibility | None:
    """``None`` only when rows may cross; otherwise the reason, for a 409.

    Symmetric: the hub asks it about a spoke's request and the spoke asks it
    about the hub's ``hello``. ``peer`` names the other side in the message.
    """
    if peer_wire is None:
        if peer_schema == state_schema.SCHEMA_VERSION:
            return None
        return Incompatibility(
            CODE_SCHEMA,
            f"{peer} sent no wire_version, so it is a build from before the "
            f"wire/schema split and can only be matched on schema: it is on "
            f"schema v{peer_schema}, this machine is on "
            f"v{state_schema.SCHEMA_VERSION}. Upgrade {peer} to a build that "
            f"sends wire_version (this one speaks wire v{WIRE_VERSION}).",
        )
    if peer_wire == WIRE_VERSION:
        return None
    return Incompatibility(
        CODE_WIRE,
        f"{peer} speaks sync wire v{peer_wire}, this machine speaks "
        f"v{WIRE_VERSION} (schema v{peer_schema} vs "
        f"v{state_schema.SCHEMA_VERSION}). The synced row shapes differ, so no "
        f"row may cross; upgrade whichever machine is on the lower wire "
        f"version, then sync again.",
    )


# ----- the shape a wire version pins -------------------------------------------

_CHECK = re.compile(r"\bCHECK\s*\(", re.IGNORECASE)
_WHERE = re.compile(r"\bWHERE\b(.*)$", re.IGNORECASE | re.DOTALL)
_COMMENT = re.compile(r"--[^\n]*")
_LAYOUT = re.compile(r"\s*([(),])\s*|\s+")


def _layout_normalized(sql: str) -> str:
    """Blind to comments and whitespace, nothing else (D-07's normalization)."""
    return _LAYOUT.sub(lambda m: m.group(1) or " ", _COMMENT.sub("", sql)).strip()


def check_clauses(create_sql: str) -> tuple[str, ...]:
    """Every ``CHECK (...)`` body in one CREATE TABLE, normalized and sorted.

    A balanced-parenthesis scan rather than a regex, because CHECK bodies nest
    (``CHECK (x IN ('a', 'b'))``). Quoted literals are skipped so a ``)``
    inside one cannot close the clause early.
    """
    sql = _COMMENT.sub("", create_sql)
    clauses: list[str] = []
    for match in _CHECK.finditer(sql):
        depth, index, quoted = 1, match.end(), False
        while depth:
            if index >= len(sql):
                raise WireShapeError(f"unbalanced CHECK clause in: {create_sql!r}")
            char = sql[index]
            if char == "'":
                quoted = not quoted
            elif not quoted:
                depth += (char == "(") - (char == ")")
            index += 1
        clauses.append(_layout_normalized(sql[match.end() : index - 1]))
    return tuple(sorted(clauses))


def _sql_of(conn: sqlite3.Connection, name: str) -> str | None:
    row = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (name,)).fetchone()
    return None if row is None or row[0] is None else str(row[0])


def _unique_indexes(conn: sqlite3.Connection, table: str) -> list[list[Any]]:
    """Each UNIQUE index as ``[columns, partial WHERE]``, name-blind."""
    found: list[list[Any]] = []
    for _seq, name, unique, _origin, _partial in conn.execute(f"PRAGMA index_list({table})"):
        if not unique:
            continue
        columns = [str(row[2]) for row in conn.execute(f'PRAGMA index_info("{name}")')]
        where = _WHERE.search(_sql_of(conn, str(name)) or "")
        found.append([columns, _layout_normalized(where.group(1)) if where else ""])
    return sorted(found)


def _table_shape(conn: sqlite3.Connection, table: str) -> dict[str, Any]:
    # ``table`` comes from protocol.DIGEST_TABLES, a constant, so the PRAGMA
    # interpolation (SQLite cannot bind identifiers) is over a closed set.
    columns = [
        [str(name), str(decl).upper(), int(notnull), int(pk)]
        for _cid, name, decl, notnull, _default, pk in conn.execute(f"PRAGMA table_info({table})")
    ]
    if not columns:
        raise WireShapeError(f"synced table {table!r} does not exist in this DB")
    create_sql = _sql_of(conn, table)
    if create_sql is None:
        raise WireShapeError(f"sqlite_master has no CREATE statement for {table!r}")
    return {
        "columns": columns,
        "foreign_keys": sorted(
            [str(row[2]), str(row[3]), str(row[4])]
            for row in conn.execute(f"PRAGMA foreign_key_list({table})")
        ),
        "checks": list(check_clauses(create_sql)),
        "unique": _unique_indexes(conn, table),
    }


def measure_wire_shape(conn: sqlite3.Connection) -> dict[str, Any]:
    """Everything a row must agree on to cross the wire, read off ``conn``."""
    shape: dict[str, Any] = {
        "tables": {table: _table_shape(conn, table) for table in protocol.DIGEST_TABLES},
        "machine_row": [field.name for field in dataclasses.fields(protocol.MachineRow)],
    }
    contract = WIRE_SEMANTIC_CONTRACTS.get(WIRE_VERSION)
    if contract is not None:
        shape["semantic_contract"] = contract
    return shape


def wire_fingerprint(shape: dict[str, Any]) -> str:
    return hashlib.sha256(protocol.canonical_bytes(shape)).hexdigest()[:16]


def fingerprint_history_problems() -> list[str]:
    """What is wrong with :data:`WIRE_FINGERPRINTS` as a HISTORY, if anything."""
    problems: list[str] = []
    expected = list(range(1, WIRE_VERSION + 1))
    if sorted(WIRE_FINGERPRINTS) != expected:
        problems.append(
            f"WIRE_FINGERPRINTS keys are {sorted(WIRE_FINGERPRINTS)}, expected "
            f"{expected}: one entry per wire version up to WIRE_VERSION, no gaps"
        )
    values = list(WIRE_FINGERPRINTS.values())
    if len(set(values)) != len(values):
        problems.append(
            "two wire versions record the same fingerprint, so one of them was not a wire change"
        )
    return problems


def wire_shape_drift(conn: sqlite3.Connection) -> str | None:
    """``None`` when ``conn``'s synced shape is the one WIRE_VERSION pins.

    Otherwise the remediation, naming the measured columns per table so the
    author can see WHAT moved. Meant for a freshly migrated DB built from
    this tree; raises :class:`WireShapeError` if it cannot measure at all.
    """
    problems = fingerprint_history_problems()
    shape = measure_wire_shape(conn)
    measured = wire_fingerprint(shape)
    recorded = WIRE_FINGERPRINTS.get(WIRE_VERSION)
    if measured != recorded:
        columns = {
            table: [column[0] for column in facts["columns"]]
            for table, facts in shape["tables"].items()
        }
        problems.append(
            f"the synced row shape measures {measured}, but WIRE_FINGERPRINTS "
            f"records {recorded} for WIRE_VERSION {WIRE_VERSION}. That is a WIRE "
            f"change (apps/sync_hub/wire_version.py docstring): set WIRE_VERSION "
            f"= {WIRE_VERSION + 1} and append {WIRE_VERSION + 1}: {measured!r} to "
            f"WIRE_FINGERPRINTS in the same commit, or undo the schema edit. "
            f"Measured columns: {json.dumps(columns, sort_keys=True)}"
        )
    return "; ".join(problems) or None


def fresh_ladder_drift() -> str | None:
    """:func:`wire_shape_drift` over a brand-new DB built by THIS tree's ladder.

    The subject the build-time guards need: every synced table is created by
    the shared-state ladder, so a fresh in-memory run of it is the shape any
    peer on this build will present.
    """
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        state_schema.apply_migrations(conn)
        return wire_shape_drift(conn)
    finally:
        conn.close()


__all__ = [
    "CODE_SCHEMA",
    "CODE_WIRE",
    "WIRE_FINGERPRINTS",
    "WIRE_SEMANTIC_CONTRACTS",
    "WIRE_VERSION",
    "Incompatibility",
    "UpdateRequiredState",
    "WireShapeError",
    "check_clauses",
    "fingerprint_history_problems",
    "fresh_ladder_drift",
    "incompatibility",
    "measure_wire_shape",
    "parse_update_required",
    "wire_fingerprint",
    "wire_shape_drift",
]
