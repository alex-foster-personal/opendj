"""The one write path for ``sync_policies`` and ``playlist_pins``.

Every policy change, from the CLI (``python -m apps.sync_hub policy ...``) or
over HTTP (``PUT``/``DELETE``/``POST /api/v1/cloudsync/policies/...``), goes
through :func:`apply_proposal`, so the two surfaces cannot drift: both stamp
through :func:`apps.shared.state.sync_stamp.stamp_and_log` inside one
:func:`~apps.shared.state.sync_stamp.stamped_transaction`, and both refuse
the same proposals. There is no SQL editing path for these tables.

Gate semantics (clean-as-you-code ratchet; a deviation from "refuse on any
error", pending the maintainer's sign-off, recorded in ``specs/cloudsync-spec.md`` D5a).
:func:`evaluate` runs :func:`apps.sync_hub.policy_rules.validate_fleet_policy`
twice: on the stored fleet, and on the fleet with the proposal applied. A
violation is ``introduced`` when the second run has it and the first does not.
It is ``blocking`` when it is an ``error`` AND either introduced OR about one
of the proposal's own targets (a cell, pin or removal it names, see
:func:`apps.sync_hub.policy_rules.proposal_targets`). So a change is refused
when it makes the fleet worse or leaves a cell it writes still broken, while
errors elsewhere (another machine still unseeded, no hub registered yet) are
reported but do not stop a machine from fixing its own cells. One carve-out:
the ``completeness`` error for a cell the proposal explicitly removes is the
requested outcome of an unset, so it is reported and not blocking.

A proposal names each cell at most once (``PROPOSAL_INVALID`` otherwise): a
cell set twice, or set and unset, has no single meaning to judge or write.

``measurable`` is False when the machines registry is empty: no per-machine
rule could judge anything, so a caller must report INCONCLUSIVE, never OK,
and :func:`apply_proposal` writes nothing.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from typing import Literal

from apps.cloud.policy import CFG as FILE_POLICY
from apps.cloud.policy import MachineClass
from apps.shared.state import sync_stamp
from apps.sync_hub.policy_rules import (
    PinCell,
    PinKey,
    PolicyCell,
    PolicyKey,
    ProposedPolicy,
    Violation,
    proposal_targets,
    validate_fleet_policy,
)

PlanAction = Literal["insert", "update", "unchanged", "delete"]
PlanTable = Literal["sync_policies", "playlist_pins"]
Row = dict[str, str | int | None]

POLICIES_TABLE: PlanTable = "sync_policies"
PINS_TABLE: PlanTable = "playlist_pins"


class PolicyInputError(ValueError):
    """A proposal that names something that does not exist, or asks for an
    operation the store cannot perform. ``status`` is the HTTP twin's code."""

    def __init__(self, code: str, status: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class CheckedViolation:
    """A :class:`Violation` annotated with what the gate made of it."""

    rule_id: str
    severity: str
    subject: str
    message: str
    introduced: bool
    blocking: bool


@dataclass(frozen=True)
class PlanEntry:
    """One row the proposal would touch. ``key`` is the asset kind or playlist id."""

    table: PlanTable
    machine_id: str
    key: str
    action: PlanAction
    before: Row | None
    after: Row | None


@dataclass(frozen=True)
class PolicyOutcome:
    """What a validate, plan or apply found. Carries no timestamps, so the
    CLI and HTTP twins can be compared byte for byte."""

    author_machine_id: str
    measurable: bool
    blocking: bool
    written: bool
    plan: tuple[PlanEntry, ...]
    violations: tuple[CheckedViolation, ...]

    def has_errors(self) -> bool:
        """True when the resulting fleet breaks any error-severity rule."""
        return any(v.severity == "error" for v in self.violations)

    def to_wire(self) -> dict[str, object]:
        """The JSON shape both twins emit."""
        return {
            "author_machine_id": self.author_machine_id,
            "measurable": self.measurable,
            "blocking": self.blocking,
            "written": self.written,
            "plan": [entry.__dict__ for entry in self.plan],
            "violations": [v.__dict__ for v in self.violations],
        }


# ----- proposal builders -----------------------------------------------------


def empty_proposal(author_machine_id: str) -> ProposedPolicy:
    """A proposal that changes nothing: validates the stored fleet as it is."""
    return ProposedPolicy(author_machine_id, (), (), (), (), (), ())


def seed_proposal(
    author_machine_id: str, machine_id: str, machine_class: MachineClass
) -> ProposedPolicy:
    """One cell per asset kind from the ``apps/cloud/policy.py`` class defaults.

    Seed input only: the synced ``sync_policies`` table stays the authority,
    and a seed is an ordinary proposal that goes through the same gate. The
    budget is carried only for ``cached``, the one mode it applies to.
    """
    cells = []
    for kind, artifact in FILE_POLICY.artifacts.items():
        mode = artifact.default_mode_by_machine_class[machine_class]
        budget = artifact.cache_budget_mb if mode == "cached" else None
        cells.append(PolicyCell(machine_id, kind, mode, budget))
    return replace(empty_proposal(author_machine_id), policies=tuple(cells))


# ----- reading ---------------------------------------------------------------


def author_machine_id(conn: sqlite3.Connection) -> str:
    """This machine's id: the author of every change made through ``conn``."""
    return sync_stamp.local_machine_id(conn)


def _live_policy(conn: sqlite3.Connection, machine_id: str, kind: str) -> Row | None:
    row = conn.execute(
        "SELECT mode, cache_budget_mb FROM sync_policies "
        "WHERE machine_id = ? AND asset_kind = ? AND deleted_at IS NULL",
        (machine_id, kind),
    ).fetchone()
    return None if row is None else {"mode": row[0], "cache_budget_mb": row[1]}


def _live_pin(conn: sqlite3.Connection, machine_id: str, playlist_id: str) -> Row | None:
    row = conn.execute(
        "SELECT mode FROM playlist_pins "
        "WHERE machine_id = ? AND playlist_id = ? AND deleted_at IS NULL",
        (machine_id, playlist_id),
    ).fetchone()
    return None if row is None else {"mode": row[0]}


def list_live(conn: sqlite3.Connection, machine_id: str | None) -> dict[str, list[Row]]:
    """Live policy cells and pins, optionally for one machine (``show``)."""
    where = "WHERE deleted_at IS NULL" + ("" if machine_id is None else " AND machine_id = ?")
    params: tuple[str, ...] = () if machine_id is None else (machine_id,)
    policies = conn.execute(
        "SELECT machine_id, asset_kind, mode, cache_budget_mb FROM sync_policies "
        f"{where} ORDER BY machine_id, asset_kind",
        params,
    ).fetchall()
    pins = conn.execute(
        f"SELECT machine_id, playlist_id, mode FROM playlist_pins {where} "
        "ORDER BY machine_id, playlist_id",
        params,
    ).fetchall()
    return {
        "policies": [
            {"machine_id": m, "asset_kind": k, "mode": mode, "cache_budget_mb": b}
            for m, k, mode, b in policies
        ],
        "pins": [{"machine_id": m, "playlist_id": p, "mode": mode} for m, p, mode in pins],
    }


# ----- gate ------------------------------------------------------------------


def _action(before: Row | None, after: Row) -> PlanAction:
    if before is None:
        return "insert"
    return "unchanged" if before == after else "update"


def plan(conn: sqlite3.Connection, proposed: ProposedPolicy) -> tuple[PlanEntry, ...]:
    """Every row the proposal touches, with its stored and proposed values."""
    entries: list[PlanEntry] = []
    for cell in proposed.policies:
        before = _live_policy(conn, cell.machine_id, cell.asset_kind)
        after: Row = {"mode": cell.mode, "cache_budget_mb": cell.cache_budget_mb}
        entries.append(
            PlanEntry(
                POLICIES_TABLE,
                cell.machine_id,
                cell.asset_kind,
                _action(before, after),
                before,
                after,
            )
        )
    for pin in proposed.pins:
        before = _live_pin(conn, pin.machine_id, pin.playlist_id)
        after = {"mode": pin.mode}
        entries.append(
            PlanEntry(
                PINS_TABLE, pin.machine_id, pin.playlist_id, _action(before, after), before, after
            )
        )
    for key in proposed.removed_policies:
        before = _live_policy(conn, key.machine_id, key.asset_kind)
        entries.append(
            PlanEntry(POLICIES_TABLE, key.machine_id, key.asset_kind, "delete", before, None)
        )
    for pin_key in proposed.removed_pins:
        before = _live_pin(conn, pin_key.machine_id, pin_key.playlist_id)
        entries.append(
            PlanEntry(PINS_TABLE, pin_key.machine_id, pin_key.playlist_id, "delete", before, None)
        )
    return tuple(entries)


@dataclass(frozen=True)
class _GateContext:
    before_keys: frozenset[tuple[str, str]]  # (rule_id, subject) on the stored fleet
    own_subjects: frozenset[str]  # every subject the proposal writes or removes
    removed_subjects: frozenset[str]  # policy cells the proposal unsets


def _check(violation: Violation, gate: _GateContext) -> CheckedViolation:
    introduced = (violation.rule_id, violation.subject) not in gate.before_keys
    own_target = violation.subject in gate.own_subjects
    expected_by_unset = (
        violation.rule_id == "completeness" and violation.subject in gate.removed_subjects
    )
    return CheckedViolation(
        violation.rule_id,
        violation.severity,
        violation.subject,
        violation.message,
        introduced,
        violation.severity == "error" and (introduced or own_target) and not expected_by_unset,
    )


def _require_distinct_keys(proposed: ProposedPolicy) -> None:
    """Each cell at most once: the validator judges the last entry for a key,
    so a repeated key would be judged on one value and written as another."""
    policy_keys = [f"{c.machine_id}/{c.asset_kind}" for c in proposed.policies]
    policy_keys += [f"{k.machine_id}/{k.asset_kind}" for k in proposed.removed_policies]
    pin_keys = [f"{p.machine_id}/{p.playlist_id}" for p in proposed.pins]
    pin_keys += [f"{k.machine_id}/{k.playlist_id}" for k in proposed.removed_pins]
    for table, keys in ((POLICIES_TABLE, policy_keys), (PINS_TABLE, pin_keys)):
        repeated = sorted({key for key in keys if keys.count(key) > 1})
        if repeated:
            raise PolicyInputError(
                "PROPOSAL_INVALID",
                422,
                f"{table}: {repeated} named more than once (set twice, or set and "
                "removed); name each cell once",
            )


def evaluate(
    conn: sqlite3.Connection,
    proposed: ProposedPolicy,
    *,
    extra_live_playlists: frozenset[str] = frozenset(),
) -> PolicyOutcome:
    """Validate and plan ``proposed``. Reads only.

    Raises :class:`PolicyInputError` (``PROPOSAL_INVALID``) for a repeated key.

    ``extra_live_playlists`` names playlists offered in the same push batch
    that are not yet in ``conn``; only the post-proposal pass consults it.
    """
    _require_distinct_keys(proposed)
    before = validate_fleet_policy(conn, empty_proposal(proposed.author_machine_id))
    after = validate_fleet_policy(
        conn, proposed, extra_live_playlists=extra_live_playlists
    )
    gate = _GateContext(
        before_keys=frozenset((v.rule_id, v.subject) for v in before),
        own_subjects=frozenset(subject for _, subject in proposal_targets(proposed)),
        removed_subjects=frozenset(
            f"{k.machine_id}/{k.asset_kind}" for k in proposed.removed_policies
        ),
    )
    checked = tuple(_check(v, gate) for v in after)
    machines = conn.execute("SELECT COUNT(*) FROM machines").fetchone()[0]
    return PolicyOutcome(
        author_machine_id=proposed.author_machine_id,
        measurable=int(machines) > 0,
        blocking=any(v.blocking for v in checked),
        written=False,
        plan=plan(conn, proposed),
        violations=checked,
    )


# ----- writing ---------------------------------------------------------------


def _require_targets(conn: sqlite3.Connection, proposed: ProposedPolicy) -> None:
    """404-class refusals, before any rule runs: the rules judge real rows."""
    _require_distinct_keys(proposed)
    if proposed.defaults or proposed.excluded_tables:
        raise PolicyInputError(
            "POLICY_NOT_APPLICABLE",
            422,
            "defaults and excluded_tables are validate/plan-only: a default is "
            "not a row and no table can leave the sync set. Propose explicit "
            "policy cells instead.",
        )
    _require_machines(conn, proposed)
    _require_playlists(conn, proposed)
    _require_live_rows(conn, proposed)


def _require_machines(conn: sqlite3.Connection, proposed: ProposedPolicy) -> None:
    machines = {str(r[0]) for r in conn.execute("SELECT machine_id FROM machines")}
    targets = [c.machine_id for c in proposed.policies] + [p.machine_id for p in proposed.pins]
    targets += [k.machine_id for k in proposed.removed_policies]
    targets += [k.machine_id for k in proposed.removed_pins]
    for machine_id in targets:
        if machine_id not in machines:
            raise PolicyInputError("MACHINE_NOT_FOUND", 404, f"unknown machine_id {machine_id!r}")


def _require_playlists(conn: sqlite3.Connection, proposed: ProposedPolicy) -> None:
    for pin in proposed.pins:
        live = conn.execute(
            "SELECT 1 FROM playlists WHERE playlist_id = ? AND deleted_at IS NULL",
            (pin.playlist_id,),
        ).fetchone()
        if live is None:
            raise PolicyInputError(
                "PLAYLIST_NOT_FOUND", 404, f"unknown playlist_id {pin.playlist_id!r}"
            )


def _require_live_rows(conn: sqlite3.Connection, proposed: ProposedPolicy) -> None:
    """An unset or unpin must name a row that is live now."""
    for key in proposed.removed_policies:
        if _live_policy(conn, key.machine_id, key.asset_kind) is None:
            raise PolicyInputError(
                "POLICY_NOT_FOUND",
                404,
                f"no live policy for {key.machine_id!r}/{key.asset_kind!r} to unset",
            )
    for pin_key in proposed.removed_pins:
        if _live_pin(conn, pin_key.machine_id, pin_key.playlist_id) is None:
            raise PolicyInputError(
                "PIN_NOT_FOUND",
                404,
                f"no live pin for {pin_key.machine_id!r}/{pin_key.playlist_id!r} to unpin",
            )


def _upsert_policy(conn: sqlite3.Connection, cell: PolicyCell, origin: str) -> None:
    stamp = sync_stamp.stamp_and_log(
        conn, POLICIES_TABLE, (cell.machine_id, cell.asset_kind), origin
    )
    conn.execute(
        """
        INSERT INTO sync_policies(
            machine_id, asset_kind, mode, cache_budget_mb,
            updated_at, origin_device_id, deleted_at
        ) VALUES (?, ?, ?, ?, ?, ?, NULL)
        ON CONFLICT(machine_id, asset_kind) DO UPDATE SET
            mode             = excluded.mode,
            cache_budget_mb  = excluded.cache_budget_mb,
            updated_at       = excluded.updated_at,
            origin_device_id = excluded.origin_device_id,
            deleted_at       = NULL
        """,
        (
            cell.machine_id,
            cell.asset_kind,
            cell.mode,
            cell.cache_budget_mb,
            stamp.updated_at,
            stamp.origin_device_id,
        ),
    )


def _upsert_pin(conn: sqlite3.Connection, pin: PinCell, origin: str) -> None:
    stamp = sync_stamp.stamp_and_log(conn, PINS_TABLE, (pin.machine_id, pin.playlist_id), origin)
    conn.execute(
        """
        INSERT INTO playlist_pins(
            machine_id, playlist_id, mode, updated_at, origin_device_id, deleted_at
        ) VALUES (?, ?, ?, ?, ?, NULL)
        ON CONFLICT(machine_id, playlist_id) DO UPDATE SET
            mode             = excluded.mode,
            updated_at       = excluded.updated_at,
            origin_device_id = excluded.origin_device_id,
            deleted_at       = NULL
        """,
        (pin.machine_id, pin.playlist_id, pin.mode, stamp.updated_at, stamp.origin_device_id),
    )


def _tombstone(
    conn: sqlite3.Connection,
    table: PlanTable,
    key_column: str,
    key: PolicyKey | PinKey,
    origin: str,
) -> None:
    """Soft-delete one row: ``deleted_at = updated_at = stamp``, logged like any write."""
    key_value = key.asset_kind if isinstance(key, PolicyKey) else key.playlist_id
    stamp = sync_stamp.stamp_and_log(conn, table, (key.machine_id, key_value), origin)
    cursor = conn.execute(
        f"UPDATE {table} SET deleted_at = ?, updated_at = ?, origin_device_id = ? "
        f"WHERE machine_id = ? AND {key_column} = ? AND deleted_at IS NULL",
        (stamp.updated_at, stamp.updated_at, stamp.origin_device_id, key.machine_id, key_value),
    )
    if cursor.rowcount != 1:
        raise RuntimeError(
            f"tombstone of {table} {key.machine_id!r}/{key_value!r} touched "
            f"{cursor.rowcount} rows, expected 1"
        )


def _write(
    conn: sqlite3.Connection, proposed: ProposedPolicy, entries: tuple[PlanEntry, ...]
) -> int:
    """Write every changed row; returns how many rows were written."""
    origin = sync_stamp.ensure_local_machine(conn)
    unchanged = {(e.table, e.machine_id, e.key) for e in entries if e.action == "unchanged"}
    written = 0
    for cell in proposed.policies:
        if (POLICIES_TABLE, cell.machine_id, cell.asset_kind) not in unchanged:
            _upsert_policy(conn, cell, origin)
            written += 1
    for pin in proposed.pins:
        if (PINS_TABLE, pin.machine_id, pin.playlist_id) not in unchanged:
            _upsert_pin(conn, pin, origin)
            written += 1
    for key in proposed.removed_policies:
        _tombstone(conn, POLICIES_TABLE, "asset_kind", key, origin)
        written += 1
    for pin_key in proposed.removed_pins:
        _tombstone(conn, PINS_TABLE, "playlist_id", pin_key, origin)
        written += 1
    return written


def apply_proposal(
    conn: sqlite3.Connection, proposed: ProposedPolicy, *, live: bool
) -> PolicyOutcome:
    """Gate ``proposed`` and, when ``live``, measurable and nothing blocks, write
    it atomically.

    Raises :class:`PolicyInputError` for a missing target or a repeated key. A
    blocking or unmeasurable outcome is RETURNED with ``written=False``; the
    caller turns it into exit 3/4 or HTTP 409. ``written`` is True only when
    at least one row was written: unchanged cells are skipped, so a re-run
    reports ``written=False``.
    """
    if not live:
        _require_targets(conn, proposed)
        return evaluate(conn, proposed)
    with sync_stamp.stamped_transaction(conn):
        _require_targets(conn, proposed)
        outcome = evaluate(conn, proposed)
        if outcome.blocking or not outcome.measurable:
            return outcome
        rows_written = _write(conn, proposed, outcome.plan)
    return replace(outcome, written=rows_written > 0)


__all__ = [
    "CheckedViolation",
    "PlanEntry",
    "PolicyInputError",
    "PolicyOutcome",
    "apply_proposal",
    "author_machine_id",
    "empty_proposal",
    "evaluate",
    "list_live",
    "plan",
    "seed_proposal",
]
