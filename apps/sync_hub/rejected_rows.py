"""Which offered rows a push lost, and why (CLOUDSYNC-31).

A push answer used to carry only ``rejected: N``. A sync that re-offered the
same rows forever (the remove, re-add and rescan loop of CLOUDSYNC-31) then
printed "accepted 0, rejected N" on every run and named nothing, so neither
a person nor an agent could tell which track was stuck. The hub now names
each rejected row in an optional ``rejected_rows`` field, which an older
client ignores and an older hub omits; the client logs every one and keeps
them on its ``SyncResult`` and in the status journal message.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from apps.sync_hub.protocol_common import SyncProtocolError

log = logging.getLogger("apps.sync_hub.client")

#: Most rejected rows one push answer names. The count stays exact; this
#: caps only the list, so one large stale offer cannot bloat the answer.
MAX_REJECTED_ROWS_NAMED: int = 100

#: Not newer than the hub's copy: lost last-writer-wins, tied it (a re-offer of a
#: row the hub already holds), or an older tombstone.
REASON_NOT_NEWER: str = "not_newer"
#: A live track the hub holds as a tombstone. The hub re-logs the tombstone,
#: so the sender's pull in the same round takes it.
REASON_REMOVED_ON_HUB: str = "removed_on_hub"
#: The content-identity collapse kept another id for the same audio.
REASON_IDENTITY_LOSER: str = "identity_loser"


@dataclass(frozen=True)
class RejectedRow:
    """One offered row the hub refused, and why."""

    table: str
    pk: tuple[str, ...]
    reason: str

    def to_wire(self) -> dict[str, Any]:
        return {"table": self.table, "pk": list(self.pk), "reason": self.reason}

    @classmethod
    def from_wire(cls, payload: object) -> RejectedRow:
        if not isinstance(payload, Mapping):
            raise SyncProtocolError(f"rejected row must be an object, got {payload!r}")
        table, pk, reason = payload.get("table"), payload.get("pk"), payload.get("reason")
        if not isinstance(table, str) or not table or not isinstance(reason, str) or not reason:
            raise SyncProtocolError(f"rejected row needs a table and a reason, got {payload!r}")
        if not isinstance(pk, list) or not pk:
            raise SyncProtocolError(f"rejected row 'pk' must be a non-empty array, got {payload!r}")
        return cls(table=table, pk=tuple(str(part) for part in pk), reason=reason)

    def describe(self) -> str:
        return f"{self.table} {list(self.pk)} ({self.reason})"


def from_push_answer(payload: Mapping[str, object]) -> list[RejectedRow]:
    """The rows one push answer names as rejected, each logged on this machine."""
    raw = payload.get("rejected_rows")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SyncProtocolError(
            f"push response 'rejected_rows' must be an array or absent, got {raw!r}"
        )
    rows = [RejectedRow.from_wire(item) for item in raw]
    for row in rows:
        log.warning("hub rejected %s", row.describe())
    return rows


def summarize(rows: Sequence[RejectedRow], *, limit: int = 5) -> str:
    """One line naming up to ``limit`` rejected rows, for a log or a status message."""
    named = "; ".join(row.describe() for row in rows[:limit])
    more = len(rows) - limit
    return f"{named}; and {more} more" if more > 0 else named


__all__ = [
    "MAX_REJECTED_ROWS_NAMED",
    "REASON_IDENTITY_LOSER",
    "REASON_NOT_NEWER",
    "REASON_REMOVED_ON_HUB",
    "RejectedRow",
    "from_push_answer",
    "summarize",
]
