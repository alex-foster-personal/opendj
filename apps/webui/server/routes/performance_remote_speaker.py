"""HTTP parity for the remote speaker (I18, REMOTE-SPK-8119..8119h).

Another Open DJ machine plays this machine's MAIN output over WebRTC. Every
control the Audio I/O panel offers is one of these routes, and the panel calls
them too, so an agent and the operator drive the same thing.

Receiver ("Be a speaker"):
  POST /speaker/start   ask the page to accept a link, then open the LAN listener
  POST /speaker/stop    close the listener and the page's link (idempotent)
  GET  /speaker         code, address, port, linked peer, status
  POST /speaker/report  page-internal: the receiving link's WebRTC state

Sender ("Send to speaker"):
  POST /link            {address, code}: the page offers, the engine relays
  POST /unlink          close the link (idempotent)
  GET  /link            status, address, error
  POST /link/offer      page-internal: relay the page's offer to the speaker
  POST /link/report     page-internal: the sending link's WebRTC state

The page does the media work through the existing command queue
(``submit_single_command``): the engine never touches audio.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, FastAPI, HTTPException, Request
from pydantic import BaseModel

from apps.webui.server import remote_speaker
from apps.webui.server.remote_speaker import (
    MAX_SDP_BYTES,
    MAX_WRONG_CODES,
    REMOTE_SPEAKER_DEFAULT_PORT,
    RelayError,
    SpeakerSession,
    parse_speaker_address,
    parse_speaker_code,
)

from .commands import page_is_open, page_is_open_on_app, submit_single_command_to_app

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/performance/remote-speaker", tags=["performance-remote-speaker"])

#: The page command each route sends, for the TS-side parity test.
REMOTE_SPEAKER_PAGE_COMMANDS: dict[str, str] = {
    "remote_speaker_receive": "POST /api/v1/performance/remote-speaker/speaker/start",
    "remote_speaker_answer": "listener POST /remote-speaker/offer",
    "remote_speaker_answer_cancel": "listener POST /remote-speaker/offer (failed, timed out or stopped)",
    "remote_speaker_receive_stop": "POST /api/v1/performance/remote-speaker/speaker/stop",
    "remote_speaker_link": "POST /api/v1/performance/remote-speaker/link",
    "remote_speaker_unlink": "POST /api/v1/performance/remote-speaker/unlink",
}

#: A page order that is not a link (enable, stop) answers in well under this.
PAGE_ORDER_TIMEOUT_S = 15.0
#: A link order gathers ICE, waits for the relay and the speaker's page, then
#: waits for the connection: 5 s + 25 s + 10 s, plus headroom.
LINK_ORDER_TIMEOUT_S = 45.0

_PAGE_REQUIRED = "performance page is not attached"
#: Shown beside every status: the speaker plays later than local MAIN.
DELAY_NOTE = (
    "Remote speaker (delayed): the speaker plays later than this machine's MAIN "
    "(WebRTC jitter buffer, inferred about 0.1-0.3 s on Wi-Fi, not measured on a LAN). "
    "Cue in your local headphones."
)

LinkStatus = Literal["idle", "linking", "linked", "failed", "disconnected"]
PeerReportState = Literal["connecting", "connected", "disconnected", "failed", "closed"]
#: Terminal peer states. "disconnected" is not one: WebRTC can recover from it
#: to "connected" on its own (a Wi-Fi blip), and a peer that does not recover
#: moves on to "failed", which is reported then.
_PEER_GONE: frozenset[str] = frozenset({"failed", "closed"})


class SpeakerStateOut(BaseModel):
    status: remote_speaker.SpeakerStatus
    code: str | None
    address: str | None
    address_error: str | None
    port: int | None
    peer: str | None
    linked_at: str | None
    wrong_codes: int
    max_wrong_codes: int
    last_error: str | None
    delay_note: str


class LinkStateOut(BaseModel):
    status: LinkStatus
    address: str | None
    linked_at: str | None
    error: str | None
    delay_note: str


class LinkAnswerOut(BaseModel):
    sdp: str


class _LinkState:
    def __init__(self) -> None:
        self.status: LinkStatus = "idle"
        self.address: str | None = None
        self.linked_at: datetime | None = None
        self.error: str | None = None
        #: Bumped by every /link and /unlink. A link attempt applies its result
        #: only while its generation is still current, so a late result never
        #: overwrites a newer link or an unlink.
        self.generation = 0
        #: The current attempt's id, carried by the page's offer and reports.
        self.link_id: str | None = None


def _start_lock(app: FastAPI) -> asyncio.Lock:
    lock = getattr(app.state, "remote_speaker_start_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        app.state.remote_speaker_start_lock = lock
    if not isinstance(lock, asyncio.Lock):
        raise TypeError("app.state.remote_speaker_start_lock has an invalid type")
    return lock


# ------------------------------------------------------------------ state --


def _session(app: FastAPI) -> SpeakerSession | None:
    session = getattr(app.state, "remote_speaker_session", None)
    if session is not None and not isinstance(session, SpeakerSession):
        raise TypeError("app.state.remote_speaker_session has an invalid type")
    return session


def _link(app: FastAPI) -> _LinkState:
    link = getattr(app.state, "remote_speaker_link", None)
    if link is None:
        link = _LinkState()
        app.state.remote_speaker_link = link
    if not isinstance(link, _LinkState):
        raise TypeError("app.state.remote_speaker_link has an invalid type")
    return link


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _speaker_out(app: FastAPI) -> SpeakerStateOut:
    session = _session(app)
    if session is None:
        return SpeakerStateOut(
            status="off",
            code=None,
            address=None,
            address_error=None,
            port=None,
            peer=None,
            linked_at=None,
            wrong_codes=0,
            max_wrong_codes=MAX_WRONG_CODES,
            last_error=None,
            delay_note=DELAY_NOTE,
        )
    return SpeakerStateOut(
        status=session.status,
        # A locked-out session's code is spent: never show it again.
        code=None if session.status == "locked_out" else session.code,
        address=session.address,
        address_error=session.address_error,
        port=session.port,
        peer=session.peer,
        linked_at=_iso(session.linked_at),
        wrong_codes=session.wrong_codes,
        max_wrong_codes=MAX_WRONG_CODES,
        last_error=session.last_error,
        delay_note=DELAY_NOTE,
    )


def _link_out(app: FastAPI) -> LinkStateOut:
    link = _link(app)
    return LinkStateOut(
        status=link.status,
        address=link.address,
        linked_at=_iso(link.linked_at),
        error=link.error,
        delay_note=DELAY_NOTE,
    )


# ----------------------------------------------------------- page orders --


def _require_page(request: Request) -> None:
    if not page_is_open(request):
        raise HTTPException(status_code=503, detail=_PAGE_REQUIRED)


async def _page_order(app: FastAPI, command: dict[str, Any], timeout_s: float) -> dict[str, Any] | None:
    """Run one page command; return its step ``result`` or raise its failure."""
    if not page_is_open_on_app(app):
        raise HTTPException(status_code=503, detail=_PAGE_REQUIRED)
    try:
        outcome = await asyncio.wait_for(submit_single_command_to_app(app, command), timeout_s)
    except TimeoutError as error:
        raise HTTPException(
            status_code=504,
            detail=f"the performance page did not finish {command['type']} within {timeout_s:g} s",
        ) from error
    steps = outcome.get("steps")
    if not isinstance(steps, list) or not steps or not isinstance(steps[0], dict):
        raise HTTPException(status_code=502, detail=f"the page sent no step result for {command['type']}")
    step = steps[0]
    if step.get("status") != "succeeded":
        failure = step.get("error")
        raise HTTPException(status_code=400, detail=str(failure) if failure is not None else "command failed")
    result = step.get("result")
    return result if isinstance(result, dict) else None


def _answerer(app: FastAPI) -> remote_speaker.AnswerOffer:
    async def answer(sdp: str, peer: str, epoch: str, link_id: str) -> str:
        result = await _page_order(
            app,
            {"type": "remote_speaker_answer", "sdp": sdp, "peer": peer, "epoch": epoch, "link_id": link_id},
            remote_speaker.ANSWER_TIMEOUT_S,
        )
        answer_sdp = None if result is None else result.get("sdp")
        if not isinstance(answer_sdp, str) or not answer_sdp.startswith("v=0"):
            raise HTTPException(status_code=502, detail="this speaker's page answered without an SDP answer")
        return answer_sdp

    return answer


def _abandoner(app: FastAPI) -> remote_speaker.AbandonAnswer:
    async def abandon(link_id: str) -> None:
        """Drop an answer the sender will never use. Best effort: the page may be gone."""
        if not page_is_open_on_app(app):
            return
        try:
            await _page_order(app, {"type": "remote_speaker_answer_cancel", "link_id": link_id}, PAGE_ORDER_TIMEOUT_S)
        except HTTPException as error:
            logger.warning("remote speaker: could not cancel answer %s: %s", link_id, error.detail)

    return abandon


async def close_remote_speaker(app: FastAPI) -> None:
    """Stop or engine quit: close the listener so its port is released.

    The session reads ``off`` first, so an answer still in flight is dropped
    rather than turned into a link nobody is tracking.
    """
    session = _session(app)
    if session is not None:
        app.state.remote_speaker_session = None
        session.status = "off"
        await session.close()


# --------------------------------------------------------------- receiver --


def _validate_port(value: object) -> int:
    if value is None:
        return REMOTE_SPEAKER_DEFAULT_PORT
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 65535:
        raise HTTPException(status_code=400, detail=f"port must be an integer 0..65535, got {value!r}")
    return value


def _validate_link_id(value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise HTTPException(status_code=400, detail="link_id must be the non-empty id of the link")
    return value


#: Longest page-reported reason kept; a report is page-internal, not a log sink.
MAX_REPORT_ERROR_CHARS = 500


def _validate_report_error(value: object) -> str | None:
    """The page's reason a link failed (a rejected playback), or None."""
    if value is None:
        return None
    if not isinstance(value, str) or value == "":
        raise HTTPException(status_code=400, detail="error must be a non-empty string when given")
    return value[:MAX_REPORT_ERROR_CHARS]


def _validate_report_state(value: object) -> PeerReportState:
    if value not in ("connecting", "connected", "disconnected", "failed", "closed"):
        raise HTTPException(
            status_code=400,
            detail=f"state must be connecting, connected, disconnected, failed or closed; got {value!r}",
        )
    return value  # type: ignore[return-value]


@router.get("/speaker", response_model=SpeakerStateOut)
async def get_speaker(request: Request) -> SpeakerStateOut:
    """The speaker session: status, code, LAN address and port, linked peer."""
    return _speaker_out(request.app)


@router.post(
    "/speaker/start",
    response_model=SpeakerStateOut,
    responses={409: {"description": "a speaker session is already running, or the port is in use"}},
)
async def post_speaker_start(
    request: Request,
    body: Annotated[dict[str, Any] | None, Body(title="RemoteSpeakerStartBody")] = None,
) -> SpeakerStateOut:
    """Be a speaker: the page agrees to receive, then the LAN listener opens.

    Optional ``port`` (default 47800; 0 picks a free one). The listener binds
    0.0.0.0 and serves only ``POST /remote-speaker/offer``; the engine API is
    not widened. The page answers first, so a build with the feature off
    refuses here and no port is opened.
    """
    _require_page(request)
    port = _validate_port((body or {}).get("port"))
    app = request.app
    # Serialized: two starts racing past the "already running" check would
    # orphan a listener (port 0) or tear down the winner's page state.
    async with _start_lock(app):
        existing = _session(app)
        if existing is not None and existing.is_open:
            raise HTTPException(status_code=409, detail="a speaker session is already running; stop it first")
        if existing is not None:
            # A locked-out session closed its own listener; it is replaced.
            app.state.remote_speaker_session = None
        session = SpeakerSession(
            code=remote_speaker.new_code(),
            answer_offer=_answerer(app),
            abandon_answer=_abandoner(app),
            requested_port=port,
        )
        await _page_order(app, {"type": "remote_speaker_receive", "epoch": session.epoch}, PAGE_ORDER_TIMEOUT_S)
        try:
            await session.open()
        except (OSError, RuntimeError) as error:
            # The page leaves receive mode: no listener, no speaker.
            await _page_order(app, {"type": "remote_speaker_receive_stop"}, PAGE_ORDER_TIMEOUT_S)
            status = 409 if isinstance(error, OSError) else 503
            raise HTTPException(
                status_code=status, detail=f"the speaker listener could not open on port {port}: {error}"
            ) from error
        app.state.remote_speaker_session = session
        return _speaker_out(app)


@router.post("/speaker/stop", response_model=SpeakerStateOut)
async def post_speaker_stop(
    request: Request,
    body: Annotated[dict[str, Any] | None, Body(title="RemoteSpeakerStopBody")] = None,
) -> SpeakerStateOut:
    """Stop being a speaker: the listener closes and the page drops its link.

    Idempotent. With no page attached the listener still closes. A stop waits
    for a start still in flight, so a start that began earlier can never
    reopen the listener after the stop has answered.

    Optional ``epoch``: stop only the session with that epoch. The page sends
    it as it leaves /performance; it has already dropped its own link, so the
    page is not ordered again, and a newer session (another page) is left on.
    """
    epoch = (body or {}).get("epoch")
    if epoch is not None and (not isinstance(epoch, str) or epoch == ""):
        raise HTTPException(status_code=400, detail="epoch must be the non-empty id of a speaker session")
    app = request.app
    async with _start_lock(app):
        if epoch is not None:
            session = _session(app)
            if session is not None and session.epoch == epoch:
                await close_remote_speaker(app)
            return _speaker_out(app)
        await close_remote_speaker(app)
        if page_is_open(request):
            await _page_order(app, {"type": "remote_speaker_receive_stop"}, PAGE_ORDER_TIMEOUT_S)
        return _speaker_out(app)


@router.post("/speaker/report", response_model=SpeakerStateOut)
async def post_speaker_report(
    request: Request,
    body: Annotated[dict[str, Any], Body(..., title="RemoteSpeakerReportBody")],
) -> SpeakerStateOut:
    """Page-internal: the receiving link's WebRTC state. A gone peer frees the session.

    ``link_id`` names the link the report is about; one about an older link is ignored.
    ``error``, when given, is why the page dropped the link (for example the
    browser refused to start playback), so a silent speaker never reads linked.
    """
    state = _validate_report_state(body.get("state"))
    link_id = _validate_link_id(body.get("link_id"))
    error = _validate_report_error(body.get("error"))
    session = _session(request.app)
    if session is not None and state in _PEER_GONE:
        session.peer_gone(link_id, state if error is None else f"{state}: {error}")
    return _speaker_out(request.app)


# ----------------------------------------------------------------- sender --


@router.get("/link", response_model=LinkStateOut)
async def get_link(request: Request) -> LinkStateOut:
    """The sending link: idle, linking, linked, failed or disconnected."""
    return _link_out(request.app)


@router.post(
    "/link",
    response_model=LinkStateOut,
    responses={
        400: {"description": "bad address or code, or the page or speaker refused (detail says which)"},
        409: {"description": "a link is already being made"},
    },
)
async def post_link(
    request: Request,
    body: Annotated[dict[str, Any], Body(..., title="RemoteSpeakerLinkBody")],
) -> LinkStateOut:
    """Send MAIN to a speaker at ``address`` (host:port) with its 6-digit ``code``."""
    _require_page(request)
    try:
        address = parse_speaker_address(body.get("address"))
        code = parse_speaker_code(body.get("code"))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    app = request.app
    link = _link(app)
    if link.status == "linking":
        raise HTTPException(status_code=409, detail=f"already linking to {link.address}")
    link.generation += 1
    generation = link.generation
    link_id = secrets.token_hex(8)
    link.status, link.address, link.error, link.linked_at, link.link_id = "linking", str(address), None, None, link_id
    try:
        await _page_order(
            app,
            {"type": "remote_speaker_link", "address": str(address), "code": code, "link_id": link_id},
            LINK_ORDER_TIMEOUT_S,
        )
    except HTTPException as error:
        if link.generation != generation:
            raise HTTPException(status_code=409, detail="superseded by a newer link or unlink") from error
        link.status, link.error = "failed", str(error.detail)
        # The page may still hold (or still be building) this connection: drop it.
        await _drop_page_link(app)
        raise
    if link.generation != generation:
        raise HTTPException(status_code=409, detail="superseded by a newer link or unlink")
    link.status, link.linked_at = "linked", datetime.now(UTC)
    return _link_out(app)


async def _drop_page_link(app: FastAPI) -> None:
    if not page_is_open_on_app(app):
        return
    try:
        await _page_order(app, {"type": "remote_speaker_unlink"}, PAGE_ORDER_TIMEOUT_S)
    except HTTPException as error:
        logger.warning("remote speaker: could not drop a failed link: %s", error.detail)


@router.post("/unlink", response_model=LinkStateOut)
async def post_unlink(
    request: Request,
    _body: Annotated[dict[str, Any] | None, Body(title="RemoteSpeakerUnlinkBody")] = None,
) -> LinkStateOut:
    """Stop sending to the speaker. Idempotent; a link attempt still in flight is superseded."""
    app = request.app
    link = _link(app)
    link.generation += 1
    link.status, link.linked_at, link.error, link.link_id = "idle", None, None, None
    if page_is_open(request):
        await _page_order(app, {"type": "remote_speaker_unlink"}, PAGE_ORDER_TIMEOUT_S)
    return _link_out(app)


@router.post(
    "/link/offer",
    response_model=LinkAnswerOut,
    responses={
        403: {"description": "the speaker refused the code"},
        409: {"description": "no link is being made"},
        502: {"description": "the speaker could not be reached or sent no answer"},
    },
)
async def post_link_offer(
    request: Request,
    body: Annotated[dict[str, Any], Body(..., title="RemoteSpeakerOfferBody")],
) -> LinkAnswerOut:
    """Page-internal: relay this page's offer to the speaker and return its answer."""
    link = _link(request.app)
    if link.status != "linking":
        raise HTTPException(status_code=409, detail="no link is being made; POST /link starts one")
    try:
        address = parse_speaker_address(body.get("address"))
        code = parse_speaker_code(body.get("code"))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if str(address) != link.address:
        raise HTTPException(status_code=409, detail=f"the link being made is to {link.address}, not {address}")
    if body.get("link_id") != link.link_id:
        raise HTTPException(status_code=409, detail="this offer belongs to a link attempt that was superseded")
    sdp = body.get("sdp")
    if not isinstance(sdp, str) or not sdp.startswith("v=0") or len(sdp.encode()) > MAX_SDP_BYTES:
        raise HTTPException(status_code=400, detail=f"sdp must be an SDP offer under {MAX_SDP_BYTES} bytes")
    try:
        answer = await remote_speaker.relay_offer(address, code, sdp)
    except RelayError as error:
        raise HTTPException(status_code=error.status, detail=error.detail) from error
    return LinkAnswerOut(sdp=answer)


@router.post("/link/report", response_model=LinkStateOut)
async def post_link_report(
    request: Request,
    body: Annotated[dict[str, Any], Body(..., title="RemoteSpeakerLinkReportBody")],
) -> LinkStateOut:
    """Page-internal: the sending link's WebRTC state after it was made.

    ``link_id`` names the link the report is about; one about an older link is ignored.
    """
    state = _validate_report_state(body.get("state"))
    link_id = _validate_link_id(body.get("link_id"))
    link = _link(request.app)
    if link.status == "linked" and state in _PEER_GONE and link_id == link.link_id:
        link.status, link.error, link.linked_at = "disconnected", f"the link {state}", None
    return _link_out(request.app)
