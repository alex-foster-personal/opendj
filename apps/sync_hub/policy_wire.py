"""JSON shapes shared by the policy CLI and its HTTP twin.

A proposal file for ``python -m apps.sync_hub policy validate|plan|apply
--proposal FILE`` is exactly the body of ``POST /api/v1/cloudsync/policies/
validate|plan``, so an agent can move a change set between the two surfaces
unchanged. Asset kinds and modes are plain strings on purpose: a bad value
is a validator violation (``unknown_asset_kind``, ``mode_not_allowed``) with
a subject and a message, not a bare 422.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from apps.sync_hub.policy_rules import (
    KindDefault,
    PinCell,
    PinKey,
    PolicyCell,
    PolicyKey,
    ProposedPolicy,
)


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PolicyCellIn(_Strict):
    machine_id: str
    asset_kind: str
    mode: str
    cache_budget_mb: int | None = None


class PinCellIn(_Strict):
    machine_id: str
    playlist_id: str
    mode: str


class PolicyKeyIn(_Strict):
    machine_id: str
    asset_kind: str


class PinKeyIn(_Strict):
    machine_id: str
    playlist_id: str


class KindDefaultIn(_Strict):
    asset_kind: str
    mode: str


class PolicyChangesIn(_Strict):
    """A change set. Every list is optional; an empty body validates the stored fleet."""

    policies: list[PolicyCellIn] = []
    pins: list[PinCellIn] = []
    removed_policies: list[PolicyKeyIn] = []
    removed_pins: list[PinKeyIn] = []
    defaults: list[KindDefaultIn] = []
    excluded_tables: list[str] = []

    def to_proposed(self, author_machine_id: str) -> ProposedPolicy:
        """The validator's view of this change set, authored by ``author_machine_id``."""
        return ProposedPolicy(
            author_machine_id=author_machine_id,
            policies=tuple(
                PolicyCell(c.machine_id, c.asset_kind, c.mode, c.cache_budget_mb)
                for c in self.policies
            ),
            pins=tuple(PinCell(p.machine_id, p.playlist_id, p.mode) for p in self.pins),
            defaults=tuple(KindDefault(d.asset_kind, d.mode) for d in self.defaults),
            excluded_tables=tuple(self.excluded_tables),
            removed_policies=tuple(
                PolicyKey(k.machine_id, k.asset_kind) for k in self.removed_policies
            ),
            removed_pins=tuple(PinKey(k.machine_id, k.playlist_id) for k in self.removed_pins),
        )


class PolicyApplyIn(_Strict):
    """``POST /policies/apply``. Dry-run unless ``dry_run`` is false, like the CLI's ``--live``."""

    changes: PolicyChangesIn
    dry_run: bool = True


class CheckedViolationOut(_Strict):
    rule_id: str
    severity: str
    subject: str
    message: str
    introduced: bool
    blocking: bool


class PlanEntryOut(_Strict):
    table: str
    machine_id: str
    key: str
    action: str
    before: dict[str, str | int | None] | None
    after: dict[str, str | int | None] | None


class PolicyOutcomeOut(_Strict):
    """:meth:`apps.sync_hub.policy_store.PolicyOutcome.to_wire`, typed."""

    author_machine_id: str
    measurable: bool
    blocking: bool
    written: bool
    plan: list[PlanEntryOut]
    violations: list[CheckedViolationOut]


__all__ = [
    "CheckedViolationOut",
    "PinCellIn",
    "PinKeyIn",
    "PlanEntryOut",
    "PolicyApplyIn",
    "PolicyCellIn",
    "PolicyChangesIn",
    "PolicyKeyIn",
    "PolicyOutcomeOut",
]
