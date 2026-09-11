"""The credential gate every bearer-authenticated sync endpoint calls.

Split out of :mod:`apps.sync_hub.service` (quality-gate file_size ratchet,
the same move ``service_enroll`` and ``service_shortfall`` made): each handler
there calls :func:`require_credential` once and this module decides what that
call means under the configured mode. The store and the verdicts live in
:mod:`apps.sync_hub.machine_credentials`; this module is only the HTTP face.

The status mapping, in one readable block:

  401  ENFORCE, and the caller's bearer is missing, wrong, revoked, or not
       owned on this hub. code: SYNC_CREDENTIAL.
  503  ENFORCE is configured but refuses to ACTIVATE: at least one machine
       is unowned or holds no credential. code: SYNC_ENFORCE_NOT_ACTIVE.
       Fail closed, not open: silently serving in observe while the
       operator believes the hub enforces is the worse failure.
  500  ``MDT_SYNC_CREDENTIAL_MODE`` holds something that is not a mode.
       code: SYNC_CREDENTIAL_MODE.

Under OBSERVE none of these fire. The verdict is logged once per machine and
verdict per process (a fleet syncing every few minutes would otherwise log
the same line on every pull chunk), and ``hello`` returns it to the caller.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from fastapi import HTTPException, Request

from apps.sync_hub import machine_credentials
from apps.sync_hub.machine_credentials import CredentialVerdict

log = logging.getLogger("apps.sync_hub.service")

#: (machine_id, verdict) pairs already logged by this process under OBSERVE.
_OBSERVED: set[tuple[str, str]] = set()

_ENVELOPE: str = 'Body: {"detail": {"code", "message"}}.'


def credential_responses(endpoint: str) -> dict[int | str, dict[str, object]]:
    """What ``endpoint`` can answer besides its 200, for the OpenAPI document.

    Description-only, like the hot-cue routes' non-2xx: the envelope is the
    standard ``HTTPException`` detail, named in the text. A body model here
    made the generated TS client repeat one 32-line block on all five
    endpoints, which the quality gate's duplication ratchet refuses.
    """
    return {
        401: {
            "description": (
                f"{endpoint} refused under ENFORCE: the Authorization bearer is "
                f"missing, wrong, revoked, or not owned on this hub. "
                f"code: SYNC_CREDENTIAL. {_ENVELOPE}"
            ),
        },
        503: {
            "description": (
                f"{endpoint} refused: ENFORCE is configured but will not activate "
                f"while any machine is unowned or holds no credential. "
                f"code: SYNC_ENFORCE_NOT_ACTIVE. {_ENVELOPE}"
            ),
        },
    }


def _refuse_to_activate(blockers: list[machine_credentials.EnforceBlocker]) -> HTTPException:
    """503 naming the COUNTS, never the machine ids: this caller is not yet
    authenticated, and the ids are exactly what it must not learn."""
    reasons: dict[str, int] = {}
    for blocker in blockers:
        reasons[blocker.reason] = reasons.get(blocker.reason, 0) + 1
    summary = ", ".join(f"{count} {reason}" for reason, count in sorted(reasons.items()))
    return HTTPException(
        status_code=503,
        detail={
            "code": "SYNC_ENFORCE_NOT_ACTIVE",
            "message": (
                f"{machine_credentials.MODE_ENV}=enforce refuses to activate "
                f"while {len(blockers)} machine(s) would be locked out "
                f"({summary}). The hub refuses sync rather than silently "
                f"serving unauthenticated. On the hub, list them with "
                f"`python -m apps.sync_hub credentials --data-dir <hub data "
                f"dir>` and enroll, adopt or revoke each one, or set "
                f"{machine_credentials.MODE_ENV}=observe."
            ),
        },
    )


def _refuse(verdict: CredentialVerdict, endpoint: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={
            "code": "SYNC_CREDENTIAL",
            "message": (
                f"{endpoint} refused: the sync credential is {verdict}. This hub "
                f"runs {machine_credentials.MODE_ENV}=enforce. A machine gets its "
                f"credential from `python -m apps.sync_hub enroll`, which stores "
                f"it at <data-dir>/sync-credential."
            ),
        },
        headers={"WWW-Authenticate": "Bearer"},
    )


def _mode() -> machine_credentials.CredentialMode:
    try:
        return machine_credentials.configured_mode()
    except machine_credentials.CredentialModeError as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "SYNC_CREDENTIAL_MODE", "message": str(exc)},
        ) from exc


def require_credential(
    request: Request,
    conn: sqlite3.Connection,
    machine_id: str,
    *,
    data_dir: Path,
    endpoint: str,
) -> CredentialVerdict:
    """Verdict for this call. Raises under ENFORCE; only reports under OBSERVE.

    Reads only, so it is safe BEFORE any write and before
    ``_require_registered``: under ENFORCE an unknown machine id and a known
    one without its credential both answer 401, so the status code does not
    tell a probe which ids are registered.
    """
    mode = _mode()
    hub_machine_id = machine_credentials.hub_machine_id_if_known(data_dir)
    verdict = machine_credentials.verify(
        conn,
        machine_id,
        request.headers.get("authorization"),
        hub_machine_id=hub_machine_id,
    )
    if mode == "observe":
        if verdict != "valid" and (machine_id, verdict) not in _OBSERVED:
            _OBSERVED.add((machine_id, verdict))
            log.warning(
                "sync credential %s for machine %s on %s (observe mode: not "
                "refused; %s=enforce would answer 401)",
                verdict,
                machine_id,
                endpoint,
                machine_credentials.MODE_ENV,
            )
        return verdict
    if mode == "enforce":
        blockers = machine_credentials.enforce_blockers(conn, hub_machine_id=hub_machine_id)
        if blockers:
            raise _refuse_to_activate(blockers)
        if verdict != "valid":
            raise _refuse(verdict, endpoint)
        return verdict
    _exhaustive: object = mode
    raise AssertionError(f"unhandled credential mode {_exhaustive!r}")


__all__ = ["CredentialVerdict", "credential_responses", "require_credential"]
