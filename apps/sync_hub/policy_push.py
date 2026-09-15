"""Validate ``sync_policies`` and ``playlist_pins`` rows offered through ``/sync/push``.

The generic LWW apply path enforces SQL constraints only. Policy tables also
carry business rules in :mod:`apps.sync_hub.policy_rules`, which the HTTP and
CLI policy surfaces already run through :func:`apps.sync_hub.policy_store.evaluate`.
This module maps push :class:`~apps.sync_hub.protocol.RowChange` batches into
that gate so the sync path cannot admit rows the application declares invalid.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from apps.sync_hub import protocol
from apps.sync_hub.policy_rules import (
    PinCell,
    PinKey,
    PolicyCell,
    PolicyKey,
    ProposedPolicy,
)
from apps.sync_hub.policy_store import PolicyOutcome, evaluate

POLICY_TABLE = "sync_policies"
PINS_TABLE = "playlist_pins"


class SyncPolicyViolationError(Exception):
    """Blocking policy violations on a push batch."""

    def __init__(self, outcome: PolicyOutcome) -> None:
        super().__init__("sync push policy violation")
        self.outcome = outcome


def _cache_budget_mb(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"cache_budget_mb must be int or null, got {value!r}")
    return value


def _is_tombstone(change: protocol.RowChange) -> bool:
    return change.values.get("deleted_at") is not None


def proposed_from_sync_changes(
    author_machine_id: str,
    changes: Sequence[protocol.RowChange],
) -> ProposedPolicy | None:
    """Extract ``sync_policies`` / ``playlist_pins`` rows from a push batch.

    Live rows become cells; tombstones become removal keys. Returns ``None`` when
    the batch touches neither policy table.
    """
    policies: list[PolicyCell] = []
    removed_policies: list[PolicyKey] = []
    pins: list[PinCell] = []
    removed_pins: list[PinKey] = []

    for change in changes:
        values = change.values
        if change.table == POLICY_TABLE:
            machine_id = str(values["machine_id"])
            asset_kind = str(values["asset_kind"])
            if _is_tombstone(change):
                removed_policies.append(PolicyKey(machine_id, asset_kind))
            else:
                policies.append(
                    PolicyCell(
                        machine_id,
                        asset_kind,
                        str(values["mode"]),
                        _cache_budget_mb(values.get("cache_budget_mb")),
                    )
                )
        elif change.table == PINS_TABLE:
            machine_id = str(values["machine_id"])
            playlist_id = str(values["playlist_id"])
            if _is_tombstone(change):
                removed_pins.append(PinKey(machine_id, playlist_id))
            else:
                pins.append(PinCell(machine_id, playlist_id, str(values["mode"])))

    if not (policies or removed_policies or pins or removed_pins):
        return None

    return ProposedPolicy(
        author_machine_id=author_machine_id,
        policies=tuple(policies),
        pins=tuple(pins),
        defaults=(),
        excluded_tables=(),
        removed_policies=tuple(removed_policies),
        removed_pins=tuple(removed_pins),
    )


def evaluate_push_policies(
    conn: sqlite3.Connection,
    author_machine_id: str,
    changes: Sequence[protocol.RowChange],
) -> PolicyOutcome | None:
    """Run :func:`~apps.sync_hub.policy_store.evaluate` when the batch has policy rows.

    Returns ``None`` when there is nothing to validate. Raises
    :class:`SyncPolicyViolationError` when the gate blocks.
    """
    proposed = proposed_from_sync_changes(author_machine_id, changes)
    if proposed is None:
        return None
    outcome = evaluate(conn, proposed)
    if outcome.blocking:
        raise SyncPolicyViolationError(outcome)
    return outcome


def blocking_violations_wire(outcome: PolicyOutcome) -> list[dict[str, object]]:
    """Wire shape for blocking violations on a refused push."""
    return [
        {
            "rule_id": v.rule_id,
            "severity": v.severity,
            "subject": v.subject,
            "message": v.message,
            "introduced": v.introduced,
            "blocking": v.blocking,
        }
        for v in outcome.violations
        if v.blocking
    ]


__all__ = [
    "SyncPolicyViolationError",
    "blocking_violations_wire",
    "evaluate_push_policies",
    "proposed_from_sync_changes",
]
