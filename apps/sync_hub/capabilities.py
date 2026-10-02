"""What one side of the sync protocol says it understands, PER REQUEST.

Round 5 gate B-1. Round 5 made an unorderable stored stamp quarantine its own
row instead of aborting the machine, which changed the hub's answer to a push
it cannot fully decide from HTTP 422 (``origin/main``) to HTTP 200 carrying
``accepted + rejected < offered``. That is right between two upgraded peers
and DESTRUCTIVE against a spoke that is not upgraded yet: ``origin/main``'s
client writes ``last_push_seq = ceiling`` unconditionally
(``apps/sync_hub/client.py::_one_round`` on ``b1044c0e8``), so the held row
falls below a fence that can never name it again while the hub, having
written nothing and logged nothing to ``hub_changelog``, cannot re-deliver
it either. Upgrading the hub first would silently drop rows on every spoke
still on main -- the same fence defect this round exists to remove, one
version apart.

**The rule is one invariant, not a table of cases: a hub answers a caller
that has not advertised :data:`QUARANTINE_V1` exactly as ``origin/main``
would.** 422, ``SYNC_PROTOCOL``, nothing partial, nothing committed. Partial
acceptance is unlocked by the caller NAMING the token and by nothing else.

Three properties worth stating, because each one is a way this could have
been built wrong:

1. **Per request, never remembered.** The obvious shape is a handshake: the
   spoke advertises at ``/hello`` and the hub stores that against its
   ``machines`` row. A stored capability is a verdict with its timestamp
   removed (``.claude/rules/verification.md``) -- a spoke rolled back to
   main still reads as capable, and the hub partial-accepts to it, which is
   precisely the loss being fixed. The advertisement therefore rides EVERY
   request that can produce a partial answer, and the hub keeps no state
   about who is capable. ``/hello`` still carries it in both directions, but
   only as DISCOVERY: it is how a spoke learns whether its hub is upgraded,
   and it is never what the hub gates on.
2. **Absent is never yes.** :func:`understands_quarantine` is affirmative --
   it answers "did the caller NAME this token", not "is there no evidence
   against it". An older build sends no field at all, and a field that is
   missing, empty, or full of tokens from some other feature all mean the
   same thing here: refuse loudly.
3. **The token is opaque and versioned.** ``quarantine/v1`` names one
   understanding: *the peer may decide fewer rows than it was offered, and
   its digest may cover fewer rows than it holds.* A later change to what a
   partial answer means gets a new token rather than a new meaning for this
   one, so a v1 spoke can never be handed a v2 answer.
"""
from __future__ import annotations

from collections.abc import Sequence

#: The caller understands a peer that decides fewer rows than it was offered
#: (``PushResponse.quarantined``), serves fewer rows than its changelog names
#: (``PullResponse.quarantined``), and hashes fewer rows than it holds
#: (``DigestResponse.quarantined``) -- and holds its own push fence
#: accordingly rather than stepping over what did not arrive.
QUARANTINE_V1: str = "quarantine/v1"

#: This hub reports how many live ``tracks`` rows it holds, so a spoke can
#: tell SEEDING an empty hub from MERGING into a library another machine
#: already put there (CLOUDSYNC-07). Only the second case can duplicate
#: overlapping recordings. Reported only: nothing refuses on it since ADR-0068.
LIBRARY_SIZE_V1: str = "library-size/v1"

#: The caller understands ``hash_pending`` on row payloads, a hub ``/hash-pending``
#: listing, and digest responses that report ``hash_pending`` separately from
#: ``quarantined`` (issue #2850, ADR-0068).
HASH_PENDING_V1: str = "hash-pending/v1"

#: The caller understands per-row identity-collapse rejections on push
#: (issue #3057).
IDENTITY_REJECT_V1: str = "identity-reject/v1"

#: Everything this build understands, advertised on every request it makes.
THIS_BUILD: tuple[str, ...] = (
    QUARANTINE_V1,
    LIBRARY_SIZE_V1,
    HASH_PENDING_V1,
    IDENTITY_REJECT_V1,
)


def understands_hash_pending(advertised: Sequence[str] | None) -> bool:
    """True only when the caller NAMED :data:`HASH_PENDING_V1`."""
    return advertised is not None and HASH_PENDING_V1 in advertised


def hash_pending_upgrade_message() -> str:
    """Operator-facing status text when the hub cannot accept hash_pending rows."""
    return (
        f"Hub needs upgrade for hash-pending sync ({HASH_PENDING_V1}); "
        "upgrade the hub before retrying."
    )


def hash_pending_refusal(
    endpoint: str, count: int, advertised: Sequence[str] | None
) -> str:
    """Message when a batch carries ``hash_pending`` rows the peer cannot read."""
    named = "nothing" if not advertised else ", ".join(sorted(advertised))
    return (
        f"{count} offered row(s) carry hash_pending=true but the caller "
        f"advertised {named} and does not understand {HASH_PENDING_V1!r}. "
        f"Refusing the whole {endpoint} instead of dropping those rows "
        f"silently. Upgrade the caller to a build that advertises "
        f"{HASH_PENDING_V1!r}, or upgrade this hub before retrying."
    )


def understands_quarantine(advertised: Sequence[str] | None) -> bool:
    """True only when the caller NAMED :data:`QUARANTINE_V1`.

    ``None`` (no field on the wire at all) and ``()`` (an empty list) are the
    same answer as an unrelated token: no. Written as a positive membership
    test on purpose -- a predicate shaped "nothing says otherwise" would read
    a build too old to have the field as capable, which is the one caller
    that must never get a partial answer.
    """
    return advertised is not None and QUARANTINE_V1 in advertised


def refusal(endpoint: str, detail: str, advertised: Sequence[str] | None) -> str:
    """The message a hub refuses an un-upgraded caller with.

    Names the offending data (``detail``) the way ``origin/main``'s 422 did,
    then says why the answer is a refusal rather than the partial result an
    upgraded caller would get -- otherwise an operator reads this as the hub
    being broken and starts repairing the wrong machine.
    """
    named = "nothing" if not advertised else ", ".join(sorted(advertised))
    return (
        f"{detail} This hub can hold those row(s) back and answer with the "
        f"rest, but the caller advertised {named} and so does not understand "
        f"a partial {endpoint} answer: a client that ignores the shortfall "
        f"records every offered row as delivered and loses the held one. "
        f"Refusing the whole {endpoint} instead. Upgrade the caller to a "
        f"build that advertises {QUARANTINE_V1!r}, or repair this hub with "
        f"`python -m apps.shared.state.normalize_stamps --live`."
    )


__all__ = [
    "HASH_PENDING_V1",
    "IDENTITY_REJECT_V1",
    "LIBRARY_SIZE_V1",
    "QUARANTINE_V1",
    "THIS_BUILD",
    "hash_pending_refusal",
    "hash_pending_upgrade_message",
    "refusal",
    "understands_hash_pending",
    "understands_quarantine",
]
