"""Quarantine ERROR reporting for sync offer and digest walks.

A pass (one offer walk, or one digest walk) groups its held-back rows by
``(table, sync_set.hold_group(reason))`` and reports each group at ERROR
ONCE PER PROCESS, with a count and example pks, then writes one INFO summary
line counting everything it held. The CloudSync scheduler re-walks the same
held rows every 60 s: on silver's library (43,982 rows held under 7,331
``tracks`` parents) one ERROR per ``(table, reason)`` per pass was still
50,385 lines, 22.8 MB, every tick, because each parent's id made its
children's reason distinct.

A group reports again the moment its membership changes (a newly held
parent, a released one) or it disappears and comes back, so the error stays
visible once per state rather than once per tick.

The logger name stays ``apps.sync_hub.engine`` because
``tests/cloudsync/test_hub_sync_round3.py`` asserts on it.
"""
from __future__ import annotations

import hashlib
import logging
import threading
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from apps.sync_hub import sync_set

log = logging.getLogger("apps.sync_hub.engine")

EXAMPLES_PER_GROUP: int = 3

_active_pass: ContextVar[QuarantinePass | None] = ContextVar(
    "quarantine_pass", default=None
)

#: ``scope -> {(table, group): membership fingerprint}`` as last reported at
#: ERROR by this process. Each pass REPLACES its scope's entry, so a group
#: that was released drops out and reports again if it ever returns.
_reported: dict[str, dict[tuple[str, str], str]] = {}
_reported_lock = threading.Lock()


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
    pks: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)

    def fingerprint(self) -> str:
        """Order-free identity of WHICH rows are held, not just how many."""
        return hashlib.sha256("\n".join(sorted(self.pks)).encode()).hexdigest()


class QuarantinePass:
    """One sync pass: group held-back rows, report new or changed groups."""

    def __init__(self, scope: str) -> None:
        self.scope = scope
        self._groups: dict[tuple[str, str], _Group] = defaultdict(_Group)

    def record(self, table: str, pk: object, reason: str) -> None:
        group_reason = sync_set.hold_group(reason)
        group = self._groups[(table, group_reason)]
        group.pks.append(str(pk))
        if len(group.examples) < EXAMPLES_PER_GROUP:
            specific = "" if reason == group_reason else f" ({reason})"
            group.examples.append(f"{pk}{specific}")

    def flush(self) -> None:
        fingerprints = {key: group.fingerprint() for key, group in self._groups.items()}
        with _reported_lock:
            already = _reported.get(self.scope, {})
            _reported[self.scope] = fingerprints
        reported = 0
        for key, group in sorted(self._groups.items()):
            if already.get(key) == fingerprints[key]:
                continue
            reported += 1
            table, reason = key
            log.error(
                "%s: %d row(s) NOT in the sync set (%s): %s. "
                "Example pk(s): %s. They will not reach any peer until %s; "
                "every other row still syncs.",
                table,
                len(group.pks),
                self.scope,
                reason,
                ", ".join(group.examples),
                _repair_for(reason),
            )
        if self._groups:
            log.info(
                "%s: %d row(s) NOT in the sync set in %d group(s); %d group(s) "
                "new or changed and reported at ERROR, %d unchanged since this "
                "process reported them and not repeated.",
                self.scope,
                sum(len(group.pks) for group in self._groups.values()),
                len(self._groups),
                reported,
                len(self._groups) - reported,
            )
        self._groups.clear()


def record_quarantine(table: str, pk: object, reason: str) -> None:
    """Record one held-back row for the active pass, or report it at once."""
    active = _active_pass.get()
    if active is not None:
        active.record(table, pk, reason)
        return
    pass_ = QuarantinePass("_standalone")
    pass_.record(table, pk, reason)
    pass_.flush()


@contextmanager
def quarantine_pass(scope: str) -> Iterator[QuarantinePass]:
    """Bound a sync walk: report its new or changed groups on exit."""
    pass_ = QuarantinePass(scope)
    token = _active_pass.set(pass_)
    try:
        yield pass_
    finally:
        pass_.flush()
        _active_pass.reset(token)


def reset_for_tests() -> None:
    """Clear a leaked pass context and everything this process reported."""
    _active_pass.set(None)
    with _reported_lock:
        _reported.clear()


__all__ = [
    "EXAMPLES_PER_GROUP",
    "QuarantinePass",
    "quarantine_pass",
    "record_quarantine",
    "reset_for_tests",
]
