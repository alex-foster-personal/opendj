"""Test-user consent for error reporting and session replay (OBS-05, OBS-06).

``GET /api/v1/telemetry/consent`` tells the page whether to ask, and what
accepting turns on: the engine's own reporting (held until accepted) and the
Session Replay loader script, whose URL is derived from the frontend DSN the
payload bundles. ``PUT`` records the answer in ``<data-dir>/telemetry-consent.json``
and flips the in-process gate so the next captured event honors it without a
restart. Declining is remembered and read as an opt-out on the next boot.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from apps.shared.paths import STATE_DB
from apps.shared.telemetry import LAST_DECISION, _client
from apps.shared.telemetry.bundled import BundledTelemetryError, load_bundled_telemetry
from apps.shared.telemetry.consent import (
    CONSENT,
    TERMS_VERSION,
    ConsentError,
    read_consent,
    replay_loader_url,
    replay_session_sample_rate,
    set_consent_granted,
    write_consent,
)

router = APIRouter(prefix="/telemetry", tags=["telemetry"])
log = logging.getLogger(__name__)

#: A checkout's way to get replay: the open-dj-fe DSN in .env.
FRONTEND_DSN_ENV: str = "SENTRY_FRONTEND_DSN"


class ConsentOut(BaseModel):
    decision: Literal["undecided", "accepted", "declined"]
    #: The version the stored answer was given against, or None.
    terms_version: str | None
    #: The version the client must show and echo back to accept.
    terms_current_version: str
    decided_at: str | None
    #: True when this engine has a live Sentry client (a packaged build, or a
    #: checkout that opted in). False means accepting changes nothing here.
    telemetry_active: bool
    #: False on an operator-explicit enable (OPENDJ_TELEMETRY=1): the gate is
    #: open by design and a decline could not close it, so the page must not
    #: offer one. True for a packaged default-on build (Codex, #3737).
    consent_required: bool
    environment: str | None
    release: str | None
    #: Session Replay loader script, or None when no frontend DSN is known.
    replay_loader_url: str | None
    replay_session_sample_rate: float
    replay_on_error_sample_rate: float = 1.0


class ConsentIn(BaseModel):
    decision: Literal["accepted", "declined"]
    terms_version: str = Field(min_length=1, max_length=64)


def _data_dir(request: Request) -> Path:
    override = getattr(request.app.state, "telemetry_consent_dir", None)
    if override is not None:
        return Path(override)
    db = Path(getattr(request.app.state, "analysis_db_path", STATE_DB))
    if db.name == "state.db" and db.parent.name == "state":
        return db.parent.parent
    return db.parent


def _frontend_dsn() -> str | None:
    env_dsn = (os.environ.get(FRONTEND_DSN_ENV) or "").strip()
    if env_dsn:
        return env_dsn
    try:
        bundled = load_bundled_telemetry(os.environ)
    except BundledTelemetryError as exc:
        log.warning("bundled telemetry unreadable while answering consent: %s", exc)
        return None
    return bundled.frontend_dsn if bundled is not None else None


def _out(request: Request) -> ConsentOut:
    record = read_consent(_data_dir(request))
    decision = LAST_DECISION.value
    active = _client() is not None
    return ConsentOut(
        decision=record.decision,
        terms_version=record.terms_version,
        terms_current_version=TERMS_VERSION,
        decided_at=record.decided_at,
        telemetry_active=active,
        consent_required=CONSENT.required,
        environment=decision.environment if decision is not None else None,
        release=decision.release if decision is not None else None,
        # A stored "accepted" never outranks THIS boot's decision: with the
        # telemetry-opt-out marker or OPENDJ_TELEMETRY=0 there is no client,
        # and the loader URL is withheld so the page loads no replay SDK
        # (Codex, #3737).
        replay_loader_url=replay_loader_url(_frontend_dsn()) if active else None,
        replay_session_sample_rate=replay_session_sample_rate(),
    )


@router.get("/consent", response_model=ConsentOut)
def get_consent(request: Request) -> ConsentOut:
    return _out(request)


@router.put("/consent", response_model=ConsentOut)
def put_consent(payload: ConsentIn, request: Request) -> ConsentOut:
    try:
        record = write_consent(
            _data_dir(request), payload.decision, terms_version=payload.terms_version
        )
    except ConsentError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    set_consent_granted(record.decision == "accepted")
    log.info("telemetry consent %s (terms %s)", record.decision, record.terms_version)
    return _out(request)
