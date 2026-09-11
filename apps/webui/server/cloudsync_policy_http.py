"""HTTP mapping for :mod:`apps.sync_hub.policy_store`.

Shared by the policy PUTs in ``routes/cloudsync.py`` and the routes in
``routes/cloudsync_policy.py`` so every policy write answers the same way:
404 for a missing target, 422 for an inapplicable proposal, and 409 with the
full outcome (violations and plan) when the gate blocks. Nothing is written
on any of them.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import HTTPException

from apps.shared.events import publish
from apps.shared.state import sync_stamp
from apps.sync_hub import policy_store
from apps.sync_hub.policy_rules import ProposedPolicy


def policy_write_responses(*not_found_codes: str) -> dict[int | str, dict[str, Any]]:
    """``responses=`` for a route that goes through :func:`apply_policy_or_raise`,
    naming only the 404 codes that route can raise.

    422 (POLICY_NOT_APPLICABLE or a malformed body) is FastAPI's own declared
    validation response; 500 CLOUDSYNC_IDENTITY_ERROR is shared, undeclared, by
    every cloudsync route.
    """
    return {
        404: {"description": " or ".join(not_found_codes) + "; nothing written"},
        409: {
            "description": "POLICY_VIOLATION (the change introduces an error, or leaves one "
            "on a cell it names) or POLICY_INCONCLUSIVE (no machines registered); "
            "detail.outcome; nothing written"
        },
    }


_EVENT_KIND: dict[str, str] = {
    policy_store.POLICIES_TABLE: "cloudsync_policy",
    policy_store.PINS_TABLE: "cloudsync_playlist_pin",
}


def author_or_raise(conn: sqlite3.Connection) -> str:
    """This machine's id, or a declared 500."""
    try:
        return policy_store.author_machine_id(conn)
    except sync_stamp.SyncStampError as exc:
        raise HTTPException(
            status_code=500, detail={"code": "CLOUDSYNC_IDENTITY_ERROR", "message": str(exc)}
        ) from exc


def _input_error(exc: policy_store.PolicyInputError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail={"code": exc.code, "message": str(exc)})


def evaluate_or_raise(
    conn: sqlite3.Connection, proposed: ProposedPolicy
) -> policy_store.PolicyOutcome:
    """:func:`policy_store.evaluate`, with a refused proposal (a repeated key) as 422."""
    try:
        return policy_store.evaluate(conn, proposed)
    except policy_store.PolicyInputError as exc:
        raise _input_error(exc) from exc


def _refusal(code: str, message: str, outcome: policy_store.PolicyOutcome) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"code": code, "message": message, "outcome": outcome.to_wire()},
    )


def apply_policy_or_raise(
    conn: sqlite3.Connection, proposed: ProposedPolicy, *, live: bool
) -> policy_store.PolicyOutcome:
    """:func:`policy_store.apply_proposal`, with refusals as HTTP errors."""
    try:
        outcome = policy_store.apply_proposal(conn, proposed, live=live)
    except policy_store.PolicyInputError as exc:
        raise _input_error(exc) from exc
    if not outcome.measurable:
        raise _refusal(
            "POLICY_INCONCLUSIVE",
            "no machines are registered, so no rule could measure; nothing was written",
            outcome,
        )
    if outcome.blocking:
        raise _refusal(
            "POLICY_VIOLATION",
            "the change introduces an error-severity policy violation, or leaves one "
            "on a cell it names; nothing was written",
            outcome,
        )
    if outcome.written:
        _publish_written(outcome)
    return outcome


def _publish_written(outcome: policy_store.PolicyOutcome) -> None:
    for table, kind in _EVENT_KIND.items():
        ids = [
            f"{e.machine_id}:{e.key}"
            for e in outcome.plan
            if e.table == table and e.action != "unchanged"
        ]
        if ids:
            publish("library.changed", {"kind": kind, "ids": ids})


__all__ = [
    "apply_policy_or_raise",
    "author_or_raise",
    "evaluate_or_raise",
    "policy_write_responses",
]
