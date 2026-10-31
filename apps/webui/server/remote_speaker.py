"""Remote speaker (I18): another Open DJ machine plays this machine's MAIN output.

Two halves, both on the engine:

- RECEIVER ("Be a speaker"): :class:`SpeakerSession` owns a 6-digit code and a
  SEPARATE, opt-in LAN listener. The listener is its own small FastAPI app on
  its own port, bound to ``0.0.0.0``, exposing ONE route, ``POST
  /remote-speaker/offer``. The main engine API stays loopback-only and is never
  widened. A correct code hands the sender's WebRTC offer to this machine's
  page (through the main app's command queue) and returns the page's answer;
  a wrong code is a 403, and the fifth wrong code closes the listener.
- SENDER ("Send to speaker"): :func:`relay_offer` forwards this machine's
  page's offer, server to server, to the speaker's listener and returns the
  answer. Only a LAN address (private, loopback, CGNAT) is accepted, never a
  global, unspecified, link-local (cloud metadata) or multicast one: this is
  a LAN feature with no cloud hop.

Media never touches the engine: the pages exchange Opus over WebRTC with host
ICE candidates only (no STUN/TURN). The engine only carries two SDP blobs.
Decision record: (private path).
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import sys
from collections.abc import Awaitable, Callable, Coroutine, Generator, MutableMapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

#: The listener's port when the caller names none. Fixed so the receiver screen
#: shows the same address:port every time; a caller may pass 0 for ephemeral.
REMOTE_SPEAKER_DEFAULT_PORT = 47800
#: The listener binds every interface: that is the point of it (LAN reach).
#: Only the listener app does; the main engine API is untouched.
REMOTE_SPEAKER_BIND_HOST = "0.0.0.0"
#: The listener's only route.
OFFER_PATH = "/remote-speaker/offer"
CODE_DIGITS = 6
#: The fifth wrong code closes the listener (lockout): 5 guesses of 10^6.
MAX_WRONG_CODES = 5
#: An SDP offer with host candidates is a few KiB; anything this large is not one.
MAX_SDP_BYTES = 64 * 1024
#: The listener reads at most this much request body, and the relay at most
#: this much answer: the SDP cap plus JSON framing, with headroom.
MAX_BODY_BYTES = 128 * 1024
#: A whole request body must arrive within this; a trickle is cut off (408).
BODY_READ_TIMEOUT_S = 5.0
#: The listener serves this many connections at once; more get a 503.
LISTENER_CONCURRENCY = 8
LISTENER_KEEP_ALIVE_S = 2
LISTENER_GRACEFUL_SHUTDOWN_S = 2
#: How long the listener waits for this machine's page to answer an offer.
ANSWER_TIMEOUT_S = 20.0
#: How long the sender's relay waits for the speaker's listener to answer.
RELAY_TIMEOUT_S = 25.0

SpeakerStatus = Literal["off", "listening", "answering", "linked", "locked_out"]

#: (offer sdp, peer host, session epoch, link id) -> answer sdp, from this machine's page.
AnswerOffer = Callable[[str, str, str, str], Awaitable[str]]
#: (link id) -> tell this machine's page to drop an answer nobody will use.
AbandonAnswer = Callable[[str], Coroutine[Any, Any, None]]


class SpeakerOfferBody(BaseModel):
    code: str
    sdp: str


class SpeakerAnswerOut(BaseModel):
    sdp: str


def new_code() -> str:
    """A uniformly random 6-digit code, zero padded."""
    return f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"


def lan_address() -> tuple[str | None, str | None]:
    """``(address, error)``: the IPv4 address this machine's LAN route leaves from.

    A UDP ``connect`` sends nothing; it only asks the OS which interface would
    carry traffic to a non-routable documentation address (RFC 5737). A machine
    with no route answers ``(None, reason)``, never a guessed address.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        address = probe.getsockname()[0]
    except OSError as error:
        return None, f"no LAN route on this machine: {error}"
    finally:
        probe.close()
    if not isinstance(address, str) or address in {"0.0.0.0", ""}:
        return None, f"the OS reported no LAN address ({address!r})"
    return address, None


def set_exclusive_bind(sock: socket.socket) -> None:
    """Platform seam: own the listener port alone.

    On Windows ``SO_REUSEADDR`` lets ANOTHER process bind the same port and
    steal connections, so Windows takes ``SO_EXCLUSIVEADDRUSE``. Elsewhere
    ``SO_REUSEADDR`` only lets a restarted speaker rebind a port still in
    TIME_WAIT; a second live listener is still refused.
    """
    if sys.platform == "win32":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


def host_is_ip_literal(host: str | None) -> bool:
    """True for ``192.0.2.4``, ``192.0.2.4:80``, ``[::1]`` or ``[::1]:80``; a name is False.

    The listener answers only requests addressed to an IP: a DNS-rebinding page
    reaches it under its own hostname, so a name in Host is refused.
    """
    if host is None or host == "":
        return False
    if host.startswith("["):
        inner, sep, rest = host[1:].partition("]")
        if sep == "" or (rest != "" and not (rest.startswith(":") and rest[1:].isdigit())):
            return False
        literal = inner
    else:
        literal, sep, port = host.rpartition(":")
        if sep == "":
            literal = host
        elif not port.isdigit():
            return False
    try:
        ipaddress.ip_address(literal)
    except ValueError:
        return False
    return True


_Scope = MutableMapping[str, Any]
_Message = MutableMapping[str, Any]
_Receive = Callable[[], Awaitable[_Message]]
_Send = Callable[[_Message], Awaitable[None]]
_Asgi = Callable[[_Scope, _Receive, _Send], Awaitable[None]]


async def _refuse(send: _Send, status: int, detail: str) -> None:
    body = ('{"detail": "' + detail.replace('"', "'") + '"}').encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})


class ListenerGuard:
    """ASGI guard in front of the listener's one route, run before any parsing.

    - an ``Origin`` header (any browser page, including a no-cors drive-by) -> 403
    - a ``Host`` that is not an IP literal (DNS rebinding) -> 403
    - a POST that is not ``application/json`` (a no-cors text/plain form) -> 415
    - a body over MAX_BODY_BYTES, declared or streamed -> 413
    - a body that does not arrive within BODY_READ_TIMEOUT_S -> 408

    The body is read here, bounded, and replayed to the app, so FastAPI never
    parses more than MAX_BODY_BYTES.
    """

    def __init__(self, app: _Asgi) -> None:
        self.app = app

    async def __call__(self, scope: _Scope, receive: _Receive, send: _Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        refusal = _header_refusal(scope)
        if refusal is not None:
            await _refuse(send, *refusal)
            return
        if scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        read = await _bounded_body(receive)
        if read is None:
            return  # the client disconnected mid-body
        if isinstance(read, tuple):
            await _refuse(send, *read)
            return
        replayed = False

        async def replay() -> _Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": read, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def _header_refusal(scope: _Scope) -> tuple[int, str] | None:
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
    if "origin" in headers:
        return 403, "browser requests are refused (Origin header present)"
    if not host_is_ip_literal(headers.get("host")):
        return 403, "Host must be an IP address, not a name"
    if scope["method"] != "POST":
        return None
    content_type = headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        return 415, "the offer must be application/json"
    declared = headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY_BYTES):
        return 413, f"the offer exceeds {MAX_BODY_BYTES} bytes"
    return None


async def _bounded_body(receive: _Receive) -> bytes | tuple[int, str] | None:
    """The whole body, a refusal (413 too big, 408 too slow), or None on disconnect."""
    chunks: list[bytes] = []
    size = 0
    try:
        async with asyncio.timeout(BODY_READ_TIMEOUT_S):
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return None
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    return 413, f"the offer exceeds {MAX_BODY_BYTES} bytes"
                chunks.append(chunk)
                if not message.get("more_body", False):
                    return b"".join(chunks)
    except TimeoutError:
        return 408, f"the offer did not arrive within {BODY_READ_TIMEOUT_S:g} s"


class _QuietServer(uvicorn.Server):
    """A uvicorn server that leaves the engine's own signal handlers alone.

    ``Server.serve`` installs SIGINT/SIGTERM handlers for its lifetime when run
    on the main thread, which would steal the engine's quit signal while a
    speaker session is listening.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None, None, None]:
        yield


@dataclass
class SpeakerSession:
    """One "Be a speaker" session: its code, its listener and who is linked."""

    code: str
    answer_offer: AnswerOffer
    abandon_answer: AbandonAnswer
    bind_host: str = REMOTE_SPEAKER_BIND_HOST
    requested_port: int = REMOTE_SPEAKER_DEFAULT_PORT
    status: SpeakerStatus = "listening"
    port: int | None = None
    address: str | None = None
    address_error: str | None = None
    wrong_codes: int = 0
    peer: str | None = None
    linked_at: datetime | None = None
    last_error: str | None = None
    #: This session's identity: the page answers only offers that carry it,
    #: so an answer still in flight when the session stops is dropped.
    epoch: str = field(default_factory=lambda: secrets.token_hex(8))
    #: The current link's id: a report naming another (older) link is ignored.
    link_id: str | None = None
    _server: _QuietServer | None = None
    _task: asyncio.Task[None] | None = None
    _socket: socket.socket | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: Fire-and-forget tasks (the lockout close, answer cancels), kept so they
    #: are not garbage collected mid-flight.
    _background: set[asyncio.Task[None]] = field(default_factory=set)

    # ------------------------------------------------------------ listener --

    async def open(self) -> None:
        """Bind the listener socket and start serving. Raises OSError on a busy port."""
        if self._task is not None:
            raise RuntimeError("speaker listener is already open")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            set_exclusive_bind(sock)
            sock.bind((self.bind_host, self.requested_port))
            sock.listen(16)
            sock.setblocking(False)
        except OSError:
            sock.close()
            raise
        self._socket = sock
        self.port = int(sock.getsockname()[1])
        self.address, self.address_error = lan_address()
        config = uvicorn.Config(
            build_listener_app(self),
            log_level="warning",
            access_log=False,
            lifespan="off",
            # One JSON route: no websocket stack is loaded for the listener.
            ws="none",
            limit_concurrency=LISTENER_CONCURRENCY,
            timeout_keep_alive=LISTENER_KEEP_ALIVE_S,
            timeout_graceful_shutdown=LISTENER_GRACEFUL_SHUTDOWN_S,
        )
        self._server = _QuietServer(config)
        self._task = asyncio.create_task(self._server.serve(sockets=[sock]))
        for _ in range(200):
            if self._server.started:
                return
            if self._task.done():
                break
            await asyncio.sleep(0.01)
        await self.close()
        raise RuntimeError("the speaker listener did not start within 2 s")

    @property
    def is_open(self) -> bool:
        return self._task is not None and not self._task.done()

    async def close(self) -> None:
        """Stop the listener and release its port. Idempotent."""
        server, task, sock = self._server, self._task, self._socket
        self._server = None
        self._task = None
        self._socket = None
        if server is not None:
            server.should_exit = True
        try:
            if task is not None:
                try:
                    await asyncio.wait_for(task, timeout=5.0)
                except TimeoutError:
                    task.cancel()
                    logger.warning("remote speaker: listener did not stop within 5 s; cancelled")
        finally:
            # Released even when the server task died with its own error.
            if sock is not None:
                sock.close()

    def _spawn(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.get_running_loop().create_task(work)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def close_soon(self) -> None:
        """Close from inside a listener request (the lockout): never await self."""
        self._spawn(self.close())

    # --------------------------------------------------------------- offer --

    def check_code(self, code: str) -> bool:
        """Constant-time code check that counts misses; the fifth miss locks out.

        Only a LISTENING session checks codes: a busy one (answering, linked)
        answers 409 before this is reached, so a guess there neither confirms
        a code nor counts toward the lockout.
        """
        if self.status != "listening":
            return False
        if hmac.compare_digest(code.encode(), self.code.encode()):
            return True
        self.wrong_codes += 1
        if self.wrong_codes >= MAX_WRONG_CODES:
            self.status = "locked_out"
            self.last_error = f"{MAX_WRONG_CODES} wrong codes: the listener closed"
            logger.warning("remote speaker: %s", self.last_error)
            self.close_soon()
        return False

    async def accept_offer(self, sdp: str, peer: str) -> str:
        """Hand a code-checked offer to this machine's page; one link at a time."""
        if self._lock.locked() or self.status in {"answering", "linked"}:
            raise HTTPException(
                status_code=409,
                detail=f"speaker is already {self.status}"
                + (f" to {self.peer}" if self.peer else "")
                + "; one link per speaker session",
            )
        async with self._lock:
            self.status = "answering"
            link_id = secrets.token_hex(8)
            try:
                answer = await asyncio.wait_for(self.answer_offer(sdp, peer, self.epoch, link_id), ANSWER_TIMEOUT_S)
            except TimeoutError as error:
                self._answer_failed(link_id, f"this speaker's page did not answer within {ANSWER_TIMEOUT_S:g} s")
                raise HTTPException(status_code=504, detail=self.last_error) from error
            except HTTPException as error:
                self._answer_failed(link_id, str(error.detail))
                raise
            except Exception as error:
                self._answer_failed(link_id, f"this speaker's page could not answer: {error}")
                raise HTTPException(status_code=502, detail=self.last_error) from error
            if self.status != "answering":
                # Stopped or replaced while the page answered: nobody gets this link.
                self._spawn(self.abandon_answer(link_id))
                raise HTTPException(status_code=409, detail="the speaker stopped while answering")
            self.status = "linked"
            self.peer = peer
            self.link_id = link_id
            self.linked_at = datetime.now(UTC)
            self.last_error = None
            return answer

    def _answer_failed(self, link_id: str, reason: str) -> None:
        """Back to listening, and tell the page to drop a connection it may still be building."""
        if self.status == "answering":
            self.status = "listening"
        self.last_error = reason
        self._spawn(self.abandon_answer(link_id))

    def peer_gone(self, link_id: str, detail: str) -> None:
        """The linked sender went away: accept a new link on the same code.

        A report about an older link (``link_id`` not the current one) is
        ignored: it must not free a session whose live link is a newer one.
        """
        if self.status != "linked" or link_id != self.link_id:
            return
        self.status = "listening"
        self.last_error = f"link to {self.peer} ended: {detail}"
        self.peer = None
        self.link_id = None
        self.linked_at = None


def build_listener_app(session: SpeakerSession) -> FastAPI:
    """The LAN listener: ONE route behind :class:`ListenerGuard`, no docs, no schema."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(ListenerGuard)

    @app.post(OFFER_PATH, response_model=SpeakerAnswerOut)
    async def post_offer(request: Request, body: SpeakerOfferBody) -> SpeakerAnswerOut:
        if len(body.sdp.encode()) > MAX_SDP_BYTES:
            raise HTTPException(status_code=413, detail=f"offer exceeds {MAX_SDP_BYTES} bytes")
        if session.status in ("answering", "linked"):
            # Before the code: a busy speaker neither confirms a code nor counts a guess.
            raise HTTPException(status_code=409, detail="speaker is busy with a link; one link per speaker session")
        if not session.check_code(body.code):
            if session.status == "locked_out":
                raise HTTPException(status_code=403, detail="wrong code; the speaker locked out")
            left = MAX_WRONG_CODES - session.wrong_codes
            raise HTTPException(status_code=403, detail=f"wrong code; {left} tries left")
        if not body.sdp.startswith("v=0"):
            raise HTTPException(status_code=400, detail="sdp is not an SDP offer (must start v=0)")
        peer = request.client.host if request.client is not None else "unknown"
        return SpeakerAnswerOut(sdp=await session.accept_offer(body.sdp, peer))

    return app


# ------------------------------------------------------------------ sender --


class RelayError(Exception):
    """The speaker's listener refused or could not be reached; ``status`` is ours to return."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class SpeakerAddress:
    host: str
    port: int

    def __str__(self) -> str:
        return f"[{self.host}]:{self.port}" if ":" in self.host else f"{self.host}:{self.port}"


def parse_speaker_address(text: object) -> SpeakerAddress:
    """``host:port`` (``[v6]:port`` for IPv6). Raises ValueError naming the defect."""
    if not isinstance(text, str) or text.strip() == "":
        raise ValueError("address must be a non-empty host:port string")
    value = text.strip()
    if value.startswith("["):
        host, sep, rest = value[1:].partition("]")
        if sep == "" or not rest.startswith(":"):
            raise ValueError(f"address must be [ipv6]:port, got {value!r}")
        port_text = rest[1:]
    else:
        host, sep, port_text = value.rpartition(":")
        if sep == "" or host == "":
            raise ValueError(f"address must be host:port, got {value!r}")
        if ":" in host:
            raise ValueError(f"an IPv6 address must be bracketed as [ipv6]:port, got {value!r}")
    if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
        raise ValueError(f"port must be 1..65535, got {port_text!r}")
    return SpeakerAddress(host=host, port=int(port_text))


def parse_speaker_code(code: object) -> str:
    if not isinstance(code, str) or len(code) != CODE_DIGITS or not code.isdigit():
        raise ValueError(f"code must be the speaker's {CODE_DIGITS}-digit code")
    return code


IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

#: Cloud metadata lives at 169.254.169.254 (link-local): refused with the rest.
_METADATA_HINT = "link-local, which includes cloud metadata at 169.254.169.254"


def unmapped(address: IpAddress) -> IpAddress:
    """``::ffff:a.b.c.d`` judged as ``a.b.c.d``.

    Before CPython 3.11.10 / 3.12.4 an IPv4-mapped IPv6 address did not
    delegate ``is_global`` and friends to its IPv4 half, so ``::ffff:8.8.8.8``
    read as non-global; ``requires-python`` admits those versions.
    """
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def lan_refusal(address: IpAddress) -> str | None:
    """Why ``address`` is not a speaker the relay may call, or None when it is."""
    judged = unmapped(address)
    if judged.is_unspecified:
        return "unspecified"
    if judged.is_link_local:
        return _METADATA_HINT
    if judged.is_multicast:
        return "multicast"
    if judged.is_global:
        return "a global internet address; remote speaker is LAN only"
    return None


async def resolve_lan_host(host: str) -> IpAddress:
    """The address the relay connects to: ``host`` itself or what it resolves to.

    Every resolved address must pass :func:`lan_refusal`; the relay then
    connects to THIS validated address, so a second lookup cannot rebind it.
    """
    try:
        addresses: list[IpAddress] = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except socket.gaierror as error:
            raise RelayError(400, f"speaker host {host!r} does not resolve: {error}") from error
        addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    if not addresses:
        raise RelayError(400, f"speaker host {host!r} resolved to no address")
    for address in addresses:
        refusal = lan_refusal(address)
        if refusal is not None:
            raise RelayError(400, f"speaker host {host!r} is {address}: {refusal}")
    return addresses[0]


async def require_lan_host(host: str) -> None:
    """Refuse any host that is, or resolves to, an address the relay may not call."""
    await resolve_lan_host(host)


async def relay_offer(address: SpeakerAddress, code: str, sdp: str) -> str:
    """Forward an offer to the speaker's listener and return its answer SDP.

    Connects to the validated IP (not the name), so Host is that IP literal,
    which is also the only Host the listener accepts. Reads at most
    MAX_BODY_BYTES of answer.
    """
    ip = await resolve_lan_host(address.host)
    target = SpeakerAddress(host=str(ip), port=address.port)
    url = f"http://{target}{OFFER_PATH}"
    # trust_env=False: a configured HTTP(S) proxy must never carry a LAN hop.
    async with httpx.AsyncClient(timeout=RELAY_TIMEOUT_S, trust_env=False) as client:
        try:
            async with client.stream("POST", url, json={"code": code, "sdp": sdp}) as response:
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_BODY_BYTES:
                        raise RelayError(502, f"speaker at {address} sent more than {MAX_BODY_BYTES} bytes")
                status = response.status_code
        except httpx.TimeoutException as error:
            raise RelayError(504, f"speaker at {address} did not answer within {RELAY_TIMEOUT_S:g} s") from error
        except httpx.HTTPError as error:
            raise RelayError(502, f"speaker at {address} is not reachable: {error}") from error
    body = _parse(bytes(raw))
    detail = _detail(body, bytes(raw))
    if status == 403:
        raise RelayError(403, f"speaker at {address} refused the code: {detail}")
    if status == 409:
        raise RelayError(409, f"speaker at {address} is busy: {detail}")
    if status != 200:
        raise RelayError(502, f"speaker at {address} answered {status}: {detail}")
    answer = body.get("sdp") if isinstance(body, dict) else None
    if not isinstance(answer, str) or not answer.startswith("v=0"):
        raise RelayError(502, f"speaker at {address} sent no answer SDP")
    return answer


def _parse(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _detail(body: Any, raw: bytes) -> str:
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])
    return raw[:200].decode("utf-8", "replace")
