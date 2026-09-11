"""CloudSync policy gate over HTTP: validate, plan, apply, unset, data classes.

The HTTP twin of ``python -m apps.sync_hub policy`` (see
:mod:`apps.sync_hub.maintenance_policy`). Both call
:mod:`apps.sync_hub.policy_store`, so the same change set gets the same
answer on either surface.

  * ``GET    /cloudsync/data-classes``                 -- the data-class registry
  * ``POST   /cloudsync/policies/validate``            -- outcome for a change set
  * ``POST   /cloudsync/policies/plan``                -- same outcome; plan-focused
  * ``POST   /cloudsync/policies/apply``               -- dry run unless dry_run=false;
    409 when blocking
  * ``DELETE /cloudsync/policies/{machine_id}/{asset_kind}``   -- tombstone (unset) a cell

Unpin over HTTP is ``POST /policies/apply`` with ``removed_pins`` (the
``policy unpin`` twin); a dedicated pin DELETE would only restate it.

A tombstone is a stamped, changelogged soft delete, so it syncs to the hub
like any other write and the digest converges.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace

from fastapi import APIRouter, Depends

from apps.sync_hub import policy_store
from apps.sync_hub.data_classes import registry_payload
from apps.sync_hub.policy_rules import PolicyKey
from apps.sync_hub.policy_wire import PolicyApplyIn, PolicyChangesIn, PolicyOutcomeOut
from apps.webui.server.cloudsync_policy_http import (
    apply_policy_or_raise,
    author_or_raise,
    evaluate_or_raise,
    policy_write_responses,
)
from apps.webui.server.routes.cloudsync import get_cloudsync_conn, get_cloudsync_write_conn

router = APIRouter(prefix="/cloudsync", tags=["cloudsync"])


def _out(outcome: policy_store.PolicyOutcome) -> PolicyOutcomeOut:
    return PolicyOutcomeOut.model_validate(outcome.to_wire())


# ``Depends(...)`` in a default is the FastAPI DI idiom; see the B008 note in
# routes/cloudsync.py for why each occurrence carries a per-line suppression.


@router.get("/data-classes", response_model=dict[str, list[dict[str, object]]])
def get_data_classes() -> dict[str, list[dict[str, object]]]:
    """Every class of data and how it syncs (``policy classes`` twin)."""
    return registry_payload()


@router.post(
    "/policies/validate",
    response_model=PolicyOutcomeOut,
    response_description="The fleet with the change set applied, judged; CLI exit 3 on any error",
)
def validate_policies(
    body: PolicyChangesIn | None = None,
    conn: sqlite3.Connection = Depends(get_cloudsync_conn),  # noqa: B008
) -> PolicyOutcomeOut:
    """The fleet with ``body`` applied, judged. No body judges the stored fleet
    (``policy validate`` without ``--proposal``)."""
    changes = PolicyChangesIn() if body is None else body
    return _out(evaluate_or_raise(conn, changes.to_proposed(author_or_raise(conn))))


@router.post(
    "/policies/plan",
    response_model=PolicyOutcomeOut,
    response_description="The rows the change set would touch; CLI exit 3 when blocking",
)
def plan_policies(
    body: PolicyChangesIn,
    conn: sqlite3.Connection = Depends(get_cloudsync_conn),  # noqa: B008
) -> PolicyOutcomeOut:
    """Rows ``body`` would touch, with the gate's verdict. Never writes."""
    return _out(evaluate_or_raise(conn, body.to_proposed(author_or_raise(conn))))


@router.post(
    "/policies/apply",
    response_model=PolicyOutcomeOut,
    response_description="Gated; written is true only when dry_run is false",
    responses=policy_write_responses(
        "MACHINE_NOT_FOUND", "PLAYLIST_NOT_FOUND", "POLICY_NOT_FOUND", "PIN_NOT_FOUND"
    ),
)
def apply_policies(
    body: PolicyApplyIn,
    conn: sqlite3.Connection = Depends(get_cloudsync_write_conn),  # noqa: B008
) -> PolicyOutcomeOut:
    """Gate the change set and, unless ``dry_run``, write it in one transaction."""
    proposed = body.changes.to_proposed(author_or_raise(conn))
    return _out(apply_policy_or_raise(conn, proposed, live=not body.dry_run))


@router.delete(
    "/policies/{machine_id}/{asset_kind}",
    response_model=PolicyOutcomeOut,
    response_description="The cell is tombstoned and will sync",
    responses=policy_write_responses("MACHINE_NOT_FOUND", "POLICY_NOT_FOUND"),
)
def delete_policy(
    machine_id: str,
    asset_kind: str,
    conn: sqlite3.Connection = Depends(get_cloudsync_write_conn),  # noqa: B008
) -> PolicyOutcomeOut:
    """Unset one cell: a synced tombstone (``policy unset --live`` twin)."""
    proposed = replace(
        policy_store.empty_proposal(author_or_raise(conn)),
        removed_policies=(PolicyKey(machine_id, asset_kind),),
    )
    return _out(apply_policy_or_raise(conn, proposed, live=True))


__all__ = ["router"]
