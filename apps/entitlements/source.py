"""WHERE a per-subject entitlement answer comes from, once one is needed.

The seam this module adds is the reason billing can plug in later without
touching sync code: a check point asks :func:`apps.entitlements.has` /
:func:`apps.entitlements.quota` / :func:`apps.entitlements.standing` with a
``subject`` and a ``source``, and never learns whether the source is a
merchant of record, a reconciled local table, or the fixed table below.

A SUBJECT is a Google ``sub`` (``users.google_sub``): the owner an enrolled
machine resolves to through ``machine_owners``. Never an email, which can
change, and never a machine id, which is not an account.

Nothing in the shipped app constructs a source. The only kind of hub that
exists today is self-hosted, and a self-hosted hub never consults one
(``apps/sync_hub/entitlement_gate.py``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from apps.entitlements.lifecycle import STATES, LifecycleState


@dataclass(frozen=True)
class Standing:
    """One subject's position for one feature.

    ``quota`` follows :func:`apps.entitlements.quota`: None is UNLIMITED,
    never "unknown".
    """

    state: LifecycleState
    quota: int | None

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise ValueError(
                f"unknown lifecycle state {self.state!r}; expected one of {list(STATES)}"
            )
        if self.quota is not None and self.quota < 0:
            raise ValueError(f"quota must be None (unlimited) or >= 0, got {self.quota}")


class EntitlementSource(Protocol):
    """Anything that can say where a subject stands for a feature.

    ``standing`` returns None when the source holds NO record for the subject
    (never subscribed). It must never be called with a missing subject: the
    resolver refuses that before it reaches the source.
    """

    @property
    def provider(self) -> str:
        """A stable name for where the answers come from, for errors and status."""
        ...

    def standing(self, subject: str, feature_id: str) -> Standing | None: ...


class StaticEntitlementSource:
    """A fixed table an operator supplies: ``{(subject, feature_id): Standing}``.

    A real source, not a stand-in: it is what a hosted hub runs before any
    merchant is wired (comped and early-access accounts), and what the hub
    gate's tests drive, the same way ``tests/cloudsync`` drives a dict-backed
    object store. A pair absent from the table is "no record", reported as
    None, never as a guessed state.
    """

    def __init__(
        self, table: Mapping[tuple[str, str], Standing], *, provider: str = "static"
    ) -> None:
        if not provider.strip():
            raise ValueError("provider must be a non-empty name")
        self._table = dict(table)
        self._provider = provider

    @property
    def provider(self) -> str:
        return self._provider

    def standing(self, subject: str, feature_id: str) -> Standing | None:
        return self._table.get((subject, feature_id))


__all__ = ["EntitlementSource", "Standing", "StaticEntitlementSource"]
