"""Fleet sync-policy validator: the rules that stop an agent breaking sync.

:func:`validate_fleet_policy` is PURE: it reads the fleet from ``conn``
(``machines``, live ``sync_policies``, live ``playlist_pins``, live
``playlists``), overlays a :class:`ProposedPolicy`, and returns every
:class:`Violation`. It never writes. An empty list means the proposal is
safe to apply; any ``error`` means it must not be.

Authority: the synced ``sync_policies`` table is the single policy authority
(pending decision D4b, recorded in ``specs/cloudsync-data-classes.md``);
``apps/cloud/policy.py`` machine-class defaults are seed input only and are
not read here.

Rules (``rule_id``):

* ``completeness``  every enrolled machine has a mode for every configurable
  class, stored or proposed, or the proposal carries an explicit default.
* ``durability``    no asset kind is excluded on every machine. pinned counts
  as a local durable copy; cached and stream count the R2 copy they read.
* ``cache_budget``  cache_budget_mb is set only when the mode is cached, and
  is positive when set.
* ``pin_target``    every pin targets a live (not tombstoned) playlist.
* ``library_non_configurable``  a sync-set table cannot be excluded: other
  rows reference it by foreign key. Excluding a table outside the sync set
  is a warned no-op; an unknown table is an error.
* ``single_hub``    exactly one machine has is_hub = 1.
* ``cross_machine_edit``  editing another machine's policy warns (refused
  once enrollment ENFORCE lands).
* ``unknown_asset_kind``  a kind outside the sync_policies CHECK is an error.
* ``unknown_machine``     a policy or pin for an unregistered machine is an error.
* ``mode_not_allowed``    a mode outside the class's allowed_modes is an error.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from apps.shared.state.migrations_v10 import ASSET_KIND_CHECK_VALUES
from apps.sync_hub.data_classes import (
    DATA_CLASSES,
    DataClassLookupError,
    class_for_asset_kind,
    class_for_table,
)
from apps.sync_hub.data_classes_types import POLICY_MODES

Severity = Literal["error", "warn"]

_SYNC_SET_MECHANISMS: frozenset[str] = frozenset({"sync_hub_changelog", "sync_hub_registry"})


@dataclass(frozen=True)
class Violation:
    """One broken rule. ``subject`` names what broke it (machine/kind, pin, table)."""

    rule_id: str
    severity: Severity
    subject: str
    message: str


@dataclass(frozen=True)
class PolicyCell:
    """A proposed ``sync_policies`` row."""

    machine_id: str
    asset_kind: str
    mode: str
    cache_budget_mb: int | None


@dataclass(frozen=True)
class PinCell:
    """A proposed ``playlist_pins`` row."""

    machine_id: str
    playlist_id: str
    mode: str


@dataclass(frozen=True)
class KindDefault:
    """An explicit fleet-wide mode for one asset kind, for machines with no row."""

    asset_kind: str
    mode: str


@dataclass(frozen=True)
class PolicyKey:
    """One ``sync_policies`` cell the proposal would tombstone (unset)."""

    machine_id: str
    asset_kind: str


@dataclass(frozen=True)
class PinKey:
    """One ``playlist_pins`` row the proposal would tombstone (unpin)."""

    machine_id: str
    playlist_id: str


@dataclass(frozen=True)
class ProposedPolicy:
    """A change set to validate against the stored fleet.

    ``author_machine_id`` is the machine making the edit. ``excluded_tables``
    names state tables the proposal would take out of the sync set.
    ``removed_policies`` and ``removed_pins`` are tombstones: the stored row
    stops counting once the proposal applies.
    """

    author_machine_id: str
    policies: tuple[PolicyCell, ...]
    pins: tuple[PinCell, ...]
    defaults: tuple[KindDefault, ...]
    excluded_tables: tuple[str, ...]
    removed_policies: tuple[PolicyKey, ...]
    removed_pins: tuple[PinKey, ...]


@dataclass(frozen=True)
class _Fleet:
    """The stored fleet with the proposal overlaid."""

    machines: dict[str, int]  # machine_id -> is_hub
    cells: dict[tuple[str, str], PolicyCell]  # (machine_id, asset_kind) -> effective row
    pins: dict[tuple[str, str], PinCell]
    live_playlists: frozenset[str]
    defaults: dict[str, str]


# ----- reading ---------------------------------------------------------------


def _read_fleet(conn: sqlite3.Connection, proposed: ProposedPolicy) -> _Fleet:
    machines = {str(m): int(h) for m, h in conn.execute("SELECT machine_id, is_hub FROM machines")}
    cells = {
        (str(m), str(k)): PolicyCell(str(m), str(k), str(mode), budget)
        for m, k, mode, budget in conn.execute(
            "SELECT machine_id, asset_kind, mode, cache_budget_mb FROM sync_policies "
            "WHERE deleted_at IS NULL"
        )
    }
    cells.update({(c.machine_id, c.asset_kind): c for c in proposed.policies})
    for removed in proposed.removed_policies:
        cells.pop((removed.machine_id, removed.asset_kind), None)
    pins = {
        (str(m), str(p)): PinCell(str(m), str(p), str(mode))
        for m, p, mode in conn.execute(
            "SELECT machine_id, playlist_id, mode FROM playlist_pins WHERE deleted_at IS NULL"
        )
    }
    pins.update({(p.machine_id, p.playlist_id): p for p in proposed.pins})
    for removed_pin in proposed.removed_pins:
        pins.pop((removed_pin.machine_id, removed_pin.playlist_id), None)
    live = frozenset(
        str(row[0])
        for row in conn.execute("SELECT playlist_id FROM playlists WHERE deleted_at IS NULL")
    )
    defaults = {d.asset_kind: d.mode for d in proposed.defaults}
    return _Fleet(machines, cells, pins, live, defaults)


def _configurable_kinds() -> tuple[str, ...]:
    return tuple(c.asset_kind for c in DATA_CLASSES if c.configurable_per_machine and c.asset_kind)


def _effective_mode(fleet: _Fleet, machine_id: str, kind: str) -> str | None:
    cell = fleet.cells.get((machine_id, kind))
    return cell.mode if cell is not None else fleet.defaults.get(kind)


# ----- rules -------------------------------------------------------------------


def _vocabulary(proposed: ProposedPolicy) -> list[Violation]:
    """unknown_asset_kind and mode_not_allowed over the proposal."""
    found: list[Violation] = []
    kinds = [(f"{c.machine_id}/{c.asset_kind}", c.asset_kind, c.mode) for c in proposed.policies]
    kinds += [(f"default/{d.asset_kind}", d.asset_kind, d.mode) for d in proposed.defaults]
    for subject, kind, mode in kinds:
        if kind not in ASSET_KIND_CHECK_VALUES:
            found.append(
                Violation(
                    "unknown_asset_kind",
                    "error",
                    subject,
                    f"{kind!r} is not one of {ASSET_KIND_CHECK_VALUES}",
                )
            )
        elif mode not in class_for_asset_kind(kind).allowed_modes:
            found.append(
                Violation(
                    "mode_not_allowed",
                    "error",
                    subject,
                    f"mode {mode!r} is not allowed for {kind!r}",
                )
            )
    found.extend(
        Violation(
            "mode_not_allowed",
            "error",
            f"{pin.machine_id}/pin/{pin.playlist_id}",
            f"pin mode {pin.mode!r} is not one of {POLICY_MODES}",
        )
        for pin in proposed.pins
        if pin.mode not in POLICY_MODES
    )
    return found


def proposal_targets(proposed: ProposedPolicy) -> list[tuple[str, str]]:
    """``(target machine_id, subject)`` for every proposed cell, pin and removal."""
    targets = [(c.machine_id, f"{c.machine_id}/{c.asset_kind}") for c in proposed.policies]
    targets += [(p.machine_id, f"{p.machine_id}/pin/{p.playlist_id}") for p in proposed.pins]
    targets += [(r.machine_id, f"{r.machine_id}/{r.asset_kind}") for r in proposed.removed_policies]
    targets += [
        (r.machine_id, f"{r.machine_id}/pin/{r.playlist_id}") for r in proposed.removed_pins
    ]
    return targets


def _unknown_machines(fleet: _Fleet, proposed: ProposedPolicy) -> list[Violation]:
    found: list[Violation] = []
    for machine_id, subject in proposal_targets(proposed):
        if machine_id not in fleet.machines:
            found.append(
                Violation(
                    "unknown_machine",
                    "error",
                    subject,
                    f"machine {machine_id!r} is not in the machines registry",
                )
            )
    return found


def _completeness(fleet: _Fleet) -> list[Violation]:
    return [
        Violation(
            "completeness",
            "error",
            f"{machine_id}/{kind}",
            f"machine {machine_id!r} has no mode for {kind!r} and the "
            "proposal carries no explicit default for it",
        )
        for machine_id in sorted(fleet.machines)
        for kind in _configurable_kinds()
        if _effective_mode(fleet, machine_id, kind) is None
    ]


def _durability(fleet: _Fleet) -> list[Violation]:
    found: list[Violation] = []
    for kind in _configurable_kinds():
        modes = [_effective_mode(fleet, m, kind) for m in sorted(fleet.machines)]
        if modes and all(mode == "excluded" for mode in modes):
            found.append(
                Violation(
                    "durability",
                    "error",
                    kind,
                    f"{kind!r} is excluded on all {len(modes)} machines, so no "
                    "durable copy exists (local or R2)",
                )
            )
    return found


def _cache_budget(fleet: _Fleet) -> list[Violation]:
    found: list[Violation] = []
    for (machine_id, kind), cell in sorted(fleet.cells.items()):
        subject = f"{machine_id}/{kind}"
        budget = cell.cache_budget_mb
        if budget is not None and cell.mode != "cached":
            found.append(
                Violation(
                    "cache_budget",
                    "error",
                    subject,
                    f"cache_budget_mb={budget} is set but mode is "
                    f"{cell.mode!r}; a budget applies only to 'cached'",
                )
            )
        elif budget is not None and budget <= 0:
            found.append(
                Violation(
                    "cache_budget", "error", subject, f"cache_budget_mb={budget} must be positive"
                )
            )
    return found


def _pin_targets(fleet: _Fleet) -> list[Violation]:
    return [
        Violation(
            "pin_target",
            "error",
            f"{machine_id}/pin/{playlist_id}",
            f"playlist {playlist_id!r} is missing or tombstoned",
        )
        for (machine_id, playlist_id) in sorted(fleet.pins)
        if playlist_id not in fleet.live_playlists
    ]


def _table_exclusions(tables: Iterable[str]) -> list[Violation]:
    found: list[Violation] = []
    for table in tables:
        try:
            data_class = class_for_table(table)
        except DataClassLookupError as exc:
            found.append(Violation("library_non_configurable", "error", table, str(exc)))
            continue
        if data_class.mechanism in _SYNC_SET_MECHANISMS:
            found.append(
                Violation(
                    "library_non_configurable",
                    "error",
                    table,
                    f"{table!r} ({data_class.id}) is in the sync set and not "
                    "configurable: other synced rows reference it by FK",
                )
            )
        elif data_class.mechanism not in _SYNC_SET_MECHANISMS:
            found.append(
                Violation(
                    "library_non_configurable",
                    "warn",
                    table,
                    f"{table!r} ({data_class.id}, {data_class.mechanism}) is "
                    "not in the sync set; excluding it is a no-op",
                )
            )
    return found


def _single_hub(fleet: _Fleet) -> list[Violation]:
    hubs = sorted(m for m, is_hub in fleet.machines.items() if is_hub == 1)
    if len(hubs) == 1:
        return []
    return [
        Violation(
            "single_hub",
            "error",
            ",".join(hubs) or "(none)",
            f"exactly one machine must have is_hub=1, found {len(hubs)}: {hubs}",
        )
    ]


def _cross_machine_edits(proposed: ProposedPolicy) -> list[Violation]:
    return [
        Violation(
            "cross_machine_edit",
            "warn",
            subject,
            f"{proposed.author_machine_id!r} is editing machine {machine_id!r}'s policy",
        )
        for machine_id, subject in proposal_targets(proposed)
        if machine_id != proposed.author_machine_id
    ]


# ----- entry point ----------------------------------------------------------------


def validate_fleet_policy(conn: sqlite3.Connection, proposed: ProposedPolicy) -> list[Violation]:
    """Every rule the stored fleet plus ``proposed`` breaks. Reads only."""
    fleet = _read_fleet(conn, proposed)
    return [
        *_vocabulary(proposed),
        *_unknown_machines(fleet, proposed),
        *_completeness(fleet),
        *_durability(fleet),
        *_cache_budget(fleet),
        *_pin_targets(fleet),
        *_table_exclusions(proposed.excluded_tables),
        *_single_hub(fleet),
        *_cross_machine_edits(proposed),
    ]


def has_errors(violations: Iterable[Violation]) -> bool:
    """True when any violation must block an apply."""
    return any(v.severity == "error" for v in violations)
