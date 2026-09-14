"""Pass-scoped quarantine ERROR aggregation for sync offer and digest walks.

One ``log.error`` per ``(table, reason)`` per pass instead of one per row.
The logger name stays ``apps.sync_hub.engine`` because
``tests/cloudsync/test_hub_sync_round3.py`` asserts on it.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator

from apps.sync_hub import sync_set

log = logging.getLogger("apps.sync_hub.engine")

_active_pass: ContextVar[QuarantinePass | None] = ContextVar(
    "quarantine_pass", default=None
)


def _repair_for(reason: str) -> str:
    """The operator next step for this hold, not always stamp repair."""
    if sync_set.IDENTITY_HOLD_REASON in reason:
        return (
            "it carries identity (`python -m apps.shared.state."
            "backfill_content_hash --live` or `/fix-links`)"
        )
    if sync_set.IDENTITY_DUP_REASON in reason:
        return "the LWW survivor of this content identity is offered instead"
    return (
        "it is repaired with `python -m apps.shared.state."
        "normalize_stamps --live`"
    )


@dataclass
class _Group:
    count: int = 0
    example_pks: list[str] = field(default_factory=list)


class QuarantinePass:
    """One sync pass: aggregate held-back rows before emitting ERROR logs."""

    def __init__(self, scope: str) -> None:
        self.scope = scope
        self._groups: dict[tuple[str, str], _Group] = defaultdict(_Group)

    def record(self, table: str, pk: object, reason: str) -> None:
        key = (table, reason)
        group = self._groups[key]
        group.count += 1
        if len(group.example_pks) < 3:
            group.example_pks.append(str(pk))

    def flush(self) -> None:
        for (table, reason), group in sorted(self._groups.items()):
            examples = ", ".join(group.example_pks)
            log.error(
                "%s: %d row(s) NOT in the sync set (%s): %s. "
                "Example pk(s): %s. They will not reach any peer until %s; "
                "every other row still syncs.",
                table,
                group.count,
                self.scope,
                reason,
                examples,
                _repair_for(reason),
            )
        self._groups.clear()


def record_quarantine(table: str, pk: object, reason: str) -> None:
    """Record one held-back row for the active pass, or emit immediately."""
    active = _active_pass.get()
    if active is not None:
        active.record(table, pk, reason)
        return
    pass_ = QuarantinePass("_standalone")
    pass_.record(table, pk, reason)
    pass_.flush()


@contextmanager
def quarantine_pass(scope: str) -> Iterator[QuarantinePass]:
    """Bound a sync walk: flush aggregated ERROR logs on exit."""
    pass_ = QuarantinePass(scope)
    token = _active_pass.set(pass_)
    try:
        yield pass_
    finally:
        pass_.flush()
        _active_pass.reset(token)


def reset_for_tests() -> None:
    """Clear any leaked pass context between tests."""
    _active_pass.set(None)


__all__ = [
    "QuarantinePass",
    "quarantine_pass",
    "record_quarantine",
    "reset_for_tests",
]
