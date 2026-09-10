"""What each partial answer HELD BACK, as prose an operator can act on.

Split out of :mod:`apps.sync_hub.service` for the quality-gate file_size
ratchet: ``/enroll`` (ADR 12) took that module past 600 lines, and these three
functions were the cleanest thing to lift -- they are the only pure functions
left in it, they share one subject, and no handler calls more than one.

All three answer the SAME question for three endpoints: this response is
short of what its caller asked for, and here is the reason and the repair.
They return ``None`` when nothing was held back, so a caller reads
"complete" as a value rather than as the absence of a message, and
:func:`apps.sync_hub.service._refuse_unless_capable` turns a non-None answer
into either a partial 200 (for a peer that advertised it understands
quarantine) or main's loud 422 (for a peer that did not).
"""
from __future__ import annotations

from apps.sync_hub import engine, protocol


def push_shortfall(result: engine.ApplyResult, offered: int) -> str | None:
    """What ``push`` held back, or None when it decided every offered row."""
    if not result.quarantined:
        return None
    return (
        f"{result.quarantined} of the {offered} offered row(s) met a local "
        f"row this hub cannot order "
        f"({protocol.describe_faults(result.faults)})."
    )


def pull_shortfall(batch: engine.ChangeBatch) -> str | None:
    """What ``pull`` could not serve, or None when the chunk is complete."""
    if not batch.quarantined:
        return None
    return (
        f"{batch.quarantined} row(s) this hub_changelog names up to seq "
        f"{batch.seq} carry a stored stamp this hub cannot order, so this "
        f"chunk is SHORT of what that seq covers."
    )


def digest_shortfall(computed: protocol.SyncDigest) -> str | None:
    """What the digest excluded, or None when it hashed everything held."""
    excluded = computed.quarantined or {}
    total = sum(excluded.values())
    if not total:
        return None
    named = ", ".join(f"{table}={count}" for table, count in sorted(excluded.items()))
    return (
        f"this digest EXCLUDED {total} row(s) this hub holds ({named}) "
        f"because their stored stamps cannot be ordered, so it does not "
        f"describe the whole table set."
    )


__all__ = ["digest_shortfall", "pull_shortfall", "push_shortfall"]
