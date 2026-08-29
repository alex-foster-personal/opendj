"""The stems relay: the only process that holds the Modal credential.

DEPLOYED, NOT SHIPPED. This module runs on a server the maintainer controls. It is in
the repo because its contract must not drift from the client's, not because
it travels in the .dmg -- nothing under ``relay/`` except ``contract.py`` and
``client.py`` is reachable from the app.

What it is for, in one line: turn "a tester I invited wants stems for this
track" into a Modal call, and refuse everything else.

Every request walks the same three gates from ``identity.py`` -- verify,
allowlist, meter -- before a single GPU-second is committed. The gates are
constructor arguments, so a deployment cannot be assembled without them and a
test cannot disable them.

SEPARATIONS ARE PER-USER. A separation id is only ever resolvable by the
caller who created it, and a lookup by anyone else is a 404, not a 403: "that
exists but is not yours" is itself a disclosure, and there is nothing here a
tester needs to learn about another tester's library.

STATE IS IN MEMORY, deliberately, for the first deployment: an allowlisted
handful of testers, a rate cap, and a restart that forgets in-flight work.
The client already treats a lost separation as a failed track and re-runs it,
so the failure mode is a re-separation, not a corrupt bundle. Swap
``_Separations`` and ``RateCap`` for a shared store when this outgrows that.

Run it:
    MDT_STEMS_RELAY_GOOGLE_CLIENT_ID=... \\
    MDT_STEMS_RELAY_ALLOWLIST=tester@example.com,other@example.com \\
    uv run --with modal uvicorn apps.stems.relay.app:build_app --factory

-Claude
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request, Response, status

from apps.stems.relay import contract as api
from apps.stems.relay.identity import (
    ALLOWLIST_ENV,
    CLIENT_ID_ENV,
    Caller,
    GoogleTokenInfoVerifier,
    IdentityError,
    IdentityVerifier,
    NotAllowlisted,
    RateCap,
    RateCapReached,
    assert_allowlisted,
    load_allowlist,
)
from apps.stems.tiers import modal_tiers

log = logging.getLogger(__name__)

# ----- CFG -------------------------------------------------------------------
MAX_SOURCE_BYTES: int = 200 * 1024 * 1024
"""Refuse a body larger than any plausible track BEFORE reading it into RAM.

A DJ mix can legitimately be an hour long, so this is generous; the point is
that an unbounded body is a way to exhaust the relay's memory without ever
touching the rate cap, because the cap counts separations, not bytes.
"""

MAX_CONCURRENT_SEPARATIONS: int = 8
MODAL_TIER_KEYS: tuple[str, ...] = tuple(tier.key for tier in modal_tiers())


@dataclass
class Separation:
    """One track's run, owned by exactly one caller."""

    separation_id: str
    stable_id: str
    owner_subject: str
    status: str = "queued"
    progress: float = 0.0
    error: str | None = None
    parts: dict[str, bytes] = field(default_factory=dict)
    audio: dict[str, Any] | None = None
    model: dict[str, str] | None = None
    preset: dict[str, Any] | None = None
    source_sha256: str | None = None

    def to_state(self) -> api.SeparationState:
        return api.SeparationState(
            separation_id=self.separation_id,
            stable_id=self.stable_id,
            status=self.status,  # type: ignore[arg-type]
            progress=self.progress,
            error=self.error,
            parts={name: len(blob) for name, blob in self.parts.items()},
            audio=api.AudioAlignment(**self.audio) if self.audio else None,
            model=self.model,
            preset=self.preset,
            source_sha256=self.source_sha256,
        )


class _Separations:
    """In-memory separation table, keyed by id, scoped by owner on read."""

    def __init__(self) -> None:
        self._rows: dict[str, Separation] = {}
        self._lock = threading.Lock()

    def put(self, row: Separation) -> None:
        with self._lock:
            self._rows[row.separation_id] = row

    def owned(self, separation_id: str, caller: Caller) -> Separation:
        """The caller's row, or a 404. Never another caller's row."""
        with self._lock:
            row = self._rows.get(separation_id)
        if row is None or row.owner_subject != caller.subject:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=api.RelayError(
                    code=api.CODE_UNKNOWN_SEPARATION,
                    message=f"no separation {separation_id!r} for this account",
                ).model_dump(),
            )
        return row


# The Modal call, isolated behind one callable so the relay can be assembled
# and tested without importing modal at all. The default is the real thing.
SeparationBackend = Callable[[bytes, str, str, str], dict[str, Any]]


def modal_backend(
    audio_bytes: bytes, stable_id: str, source_name: str, tier_key: str
) -> dict[str, Any]:
    """Separate one track on Modal, with THIS process's credential.

    The import is function-local because ``modal`` is not a repo dependency
    and the relay is deployed with ``uv run --with modal``; importing it at
    module scope would make the whole module unimportable anywhere else,
    including in the tests that check the gates.
    """
    import scripts.modal_vocal_farm as farm
    from apps.stems.tiers import get_tier

    preset = farm._resolve_preset(get_tier(tier_key).preset_tag, False)
    with farm.app.run():
        result = farm.separate_track.remote(
            audio_bytes,
            stable_id,
            source_name,
            preset.model,
            preset.overlap,
            preset.shifts,
            "local",  # return the stem bytes to this process
            source_name,
            preset.stamp(),
            "",
            farm.DEFAULT_STEM_CODEC,
        )
    if result.get("error"):
        raise RuntimeError(result["error"])
    result["preset"] = preset.stamp()
    result["model"] = {"name": preset.model, "version": farm.DEMUCS_VERSION}
    return result


def resolve_caller(
    authorization: str | None,
    verifier: IdentityVerifier,
    allowlist: frozenset[str],
) -> Caller:
    """Gates 1 and 2: who is this, and did we invite them.

    Gate 3 (the rate cap) is charged at dispatch rather than here, because a
    read of your own quota must not itself consume quota.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=api.RelayError(
                code=api.CODE_NO_TOKEN,
                message="a Google identity token is required",
            ).model_dump(),
        )
    token = authorization.split(" ", 1)[1]
    try:
        caller = verifier.verify(token)
    except IdentityError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=api.RelayError(
                code=api.CODE_BAD_TOKEN, message=str(exc)
            ).model_dump(),
        ) from exc
    except RuntimeError as exc:
        # Could not CHECK the token. A different fact from the token being
        # bad, and telling a tester their login is broken when Google is
        # unreachable sends them to fix the one thing that is fine.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=api.RelayError(
                code=api.CODE_UPSTREAM, message=str(exc)
            ).model_dump(),
        ) from exc
    try:
        assert_allowlisted(caller, allowlist)
    except NotAllowlisted as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=api.RelayError(
                code=api.CODE_NOT_ALLOWED, message=str(exc)
            ).model_dump(),
        ) from exc
    return caller


def build_router(
    *,
    verifier: IdentityVerifier,
    allowlist: frozenset[str],
    rate_cap: RateCap,
    backend: SeparationBackend,
    pool: ThreadPoolExecutor,
    store: _Separations,
) -> APIRouter:
    router = APIRouter()

    def _caller(authorization: str | None) -> Caller:
        return resolve_caller(authorization, verifier, allowlist)

    @router.get(api.PATH_HEALTH)
    def health() -> dict[str, Any]:
        """No auth: a reachability probe must not need a tester's token."""
        return {
            "status": "ok",
            "testers_allowlisted": len(allowlist),
            "tracks_per_window": rate_cap.limit,
            "window_hours": rate_cap.window.total_seconds() / 3600,
        }

    @router.get(api.PATH_QUOTA, response_model=api.QuotaState)
    def quota(authorization: str | None = Header(default=None)) -> api.QuotaState:
        caller = _caller(authorization)
        used, resets_at = rate_cap.state(caller.email)
        return api.QuotaState(
            email=caller.email,
            used=used,
            limit=rate_cap.limit,
            resets_at=resets_at.isoformat(timespec="seconds"),
            tiers_allowed=list(MODAL_TIER_KEYS),
        )

    @router.post(
        api.PATH_SEPARATIONS,
        response_model=api.SeparationCreated,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def start_separation(
        request: Request,
        authorization: str | None = Header(default=None),
        x_stems_tier: str = Header(default="M"),
        x_stems_stable_id: str = Header(default=""),
        x_stems_source_sha256: str | None = Header(default=None),
        x_stems_source_name: str = Header(default="track"),
    ) -> api.SeparationCreated:
        caller = _caller(authorization)
        if x_stems_tier not in MODAL_TIER_KEYS:
            raise _bad_request(
                f"tier {x_stems_tier!r} is not offered; known: {list(MODAL_TIER_KEYS)}"
            )
        if not x_stems_stable_id.strip():
            raise _bad_request(f"{api.HEADER_STABLE_ID} is required")

        audio_bytes = await _read_source_audio(request)
        _charge_or_refuse(rate_cap, caller.email)

        row = Separation(
            separation_id=str(uuid.uuid4()),
            stable_id=x_stems_stable_id.strip(),
            owner_subject=caller.subject,
            source_sha256=x_stems_source_sha256,
        )
        store.put(row)
        pool.submit(
            _run, row, audio_bytes, x_stems_source_name, x_stems_tier, backend
        )
        return api.SeparationCreated(
            separation_id=row.separation_id,
            stable_id=row.stable_id,
            status="queued",
        )

    @router.get(api.PATH_SEPARATION, response_model=api.SeparationState)
    def separation_state(
        separation_id: str, authorization: str | None = Header(default=None)
    ) -> api.SeparationState:
        return store.owned(separation_id, _caller(authorization)).to_state()

    @router.get(api.PATH_SEPARATION_PART)
    def separation_part(
        separation_id: str,
        part: str,
        authorization: str | None = Header(default=None),
    ) -> Response:
        row = store.owned(separation_id, _caller(authorization))
        if row.status != "succeeded":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=api.RelayError(
                    code=api.CODE_NOT_READY,
                    message=f"separation is {row.status}, not succeeded",
                ).model_dump(),
            )
        blob = row.parts.get(part)
        if blob is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=api.RelayError(
                    code=api.CODE_UNKNOWN_SEPARATION,
                    message=f"no part {part!r}; have {sorted(row.parts)}",
                ).model_dump(),
            )
        return Response(content=blob, media_type="audio/flac")

    return router


async def _read_source_audio(request: Request) -> bytes:
    """The request body, refused before it can exhaust memory.

    Checked twice, and the first check is the one that matters: a declared
    Content-Length lets the relay refuse BEFORE reading, where reading is the
    thing that costs. The second catches a chunked body that declared nothing.
    An unbounded body is a way to exhaust this process without ever touching
    the rate cap, which counts separations rather than bytes.
    """
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_SOURCE_BYTES:
        raise _too_large()
    audio_bytes = await request.body()
    if not audio_bytes:
        raise _bad_request("the request body must be the source audio")
    if len(audio_bytes) > MAX_SOURCE_BYTES:
        raise _too_large()
    return audio_bytes


def _charge_or_refuse(rate_cap: RateCap, email: str) -> None:
    """GATE 3, charged BEFORE dispatch rather than after success.

    A separation that fails still bought GPU time, so refunding it would let
    one reliably-broken file spend without limit.
    """
    try:
        rate_cap.charge(email)
    except RateCapReached as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=api.RelayError(
                code=api.CODE_QUOTA_SPENT,
                message=str(exc),
                retry_after_s=max(
                    0.0, (exc.resets_at - datetime.now(UTC)).total_seconds()
                ),
            ).model_dump(),
        ) from exc


def _too_large() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        detail=api.RelayError(
            code=api.CODE_BAD_REQUEST,
            message=f"source audio exceeds {MAX_SOURCE_BYTES} bytes",
        ).model_dump(),
    )


def _bad_request(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=api.RelayError(
            code=api.CODE_BAD_REQUEST, message=message
        ).model_dump(),
    )


def _require_every_part(result: dict[str, Any]) -> dict[str, bytes]:
    """A short part set is a failure HERE, not a bundle the client writes.

    The stem reader validates a bundle at play time, so letting three parts
    through would move the failure from a job that can be re-run to a deck
    that will not load.
    """
    stems = result.get("stems") or {}
    if set(stems) != set(api.STEM_PARTS):
        raise RuntimeError(
            f"backend returned parts {sorted(stems)}, expected "
            f"{sorted(api.STEM_PARTS)}"
        )
    return stems


def _run(
    row: Separation,
    audio_bytes: bytes,
    source_name: str,
    tier_key: str,
    backend: SeparationBackend,
) -> None:
    """Do the separation and record it. Never raises into the pool."""
    row.status = "running"
    row.progress = 0.05
    try:
        result = backend(audio_bytes, row.stable_id, source_name, tier_key)
        stems = _require_every_part(result)
        row.parts = dict(stems)
        row.audio = result["audio"]
        row.model = result.get("model")
        row.preset = result.get("preset")
        row.source_sha256 = result.get("source_sha256") or row.source_sha256
        row.progress = 1.0
        row.status = "succeeded"
    except Exception as exc:  # a worker thread must not die silently
        log.exception("separation %s failed", row.separation_id)
        row.error = f"{type(exc).__name__}: {exc}"
        row.status = "failed"
        row.progress = 1.0


def build_app(
    *,
    verifier: IdentityVerifier | None = None,
    allowlist: frozenset[str] | None = None,
    rate_cap: RateCap | None = None,
    backend: SeparationBackend | None = None,
) -> FastAPI:
    """Assemble the relay. Every gate is required; none can be turned off.

    The keyword arguments exist so tests can inject a verifier and a backend,
    NOT so a deployment can omit them: leaving them unset resolves the real
    ones from the environment and raises if the environment is incomplete.
    """
    resolved_allowlist = allowlist if allowlist is not None else load_allowlist()
    resolved_verifier = verifier or GoogleTokenInfoVerifier(
        os.environ.get(CLIENT_ID_ENV, "")
    )
    app = FastAPI(title="open-dj stems relay", version="1")
    app.include_router(
        build_router(
            verifier=resolved_verifier,
            allowlist=resolved_allowlist,
            rate_cap=rate_cap or RateCap(),
            backend=backend or modal_backend,
            pool=ThreadPoolExecutor(max_workers=MAX_CONCURRENT_SEPARATIONS),
            store=_Separations(),
        )
    )
    log.info(
        "stems relay assembled: %d allowlisted tester(s), env %s / %s",
        len(resolved_allowlist),
        CLIENT_ID_ENV,
        ALLOWLIST_ENV,
    )
    return app


__all__ = [
    "MAX_CONCURRENT_SEPARATIONS",
    "MAX_SOURCE_BYTES",
    "MODAL_TIER_KEYS",
    "Separation",
    "SeparationBackend",
    "build_app",
    "build_router",
    "modal_backend",
]
