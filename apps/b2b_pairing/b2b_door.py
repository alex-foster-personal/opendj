"""B2BPAIR-01..03: the engine's LAN door for a paired B2B partner.

The engine itself stays bound to 127.0.0.1. Once paired (or while an invite is open) the
engine runs this small server on the LAN, the in-process successor of the stand-alone
``peer_listener`` (LINK-25), with two halves:

- **Paired reads** (bearer token from the pairing): exactly the allowlist ``peer_listener``
  forwards to the loopback engine (``peer_listener.ALLOWED``): ``/api/v1/b2b/now-playing``,
  ``/api/v1/b2b/shouts/outbox``, ``/api/v1/b2b/cursor/latest`` (B2BCURSOR-01),
  ``/api/v1/b2b/multicast/self`` (B2BMCAST-03), ``/api/v1/b2b/partner-audio/outbox``
  (B2BAUDIO-02) and the virtual USB export routes (VUSB-02/03, audio streamed
  with Range forwarded, GET or HEAD). Read-only; the token is compared in constant time and
  read per request, so Forget revokes it at once.
- **Pairing** (no token, only while an invite is open): ``GET /b2b-pair/hello`` and the two
  SPAKE2 legs ``POST /b2b-pair/start`` and ``POST /b2b-pair/finish``. With no open invite
  they answer 410.

- **Nearby** (B2BNEAR-02..04, no token): ``/b2b-nearby/*``, answered by the nearby service
  (``b2b_nearby``) when it runs: Ask-to-B2B requests, their answers, and Reconnect. With the
  nearby service stopped they answer 404. A Reconnect is told which of this door's addresses
  it was dialed on (B2BREADDR-03).
- **Shared booth** (B2BBOOTH-91, bearer token): ``POST /b2b-booth/ops`` and ``/b2b-booth/ctl``, the
  booth engine-to-engine lanes, answered by the booth (``host.booth.door_post``) with bodies up to
  ``MAX_BOOTH_BODY``. 401 without the pairing token, 404 while no booth is attached.
- **Link relay** (B2BRELAY-01, sealed): ``POST /b2b-relay/ping`` and ``/b2b-relay/state``,
  answered by the relay (``link_relay``). No bearer header: each frame is AES-GCM sealed under a
  key derived from the pairing token, so the token never crosses the wire. 404 while no relay runs.

The handler speaks HTTP/1.1 keep-alive with Nagle off: the relay's clock pings must not pay a
TCP handshake (it biases a ping by half a round trip) or a delayed-ACK stall (40 ms).

Anything else is 404; bodies over ``MAX_BODY`` are 413.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import socket
import sys
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from apps.b2b_pairing.b2b_pairing import PROTOCOL_VERSION, PairingError
from apps.b2b_pairing.peer_listener import admit_peer_read, relay_engine_response

MAX_BODY = 8192
HELLO = "/b2b-pair/hello"
START = "/b2b-pair/start"
FINISH = "/b2b-pair/finish"
NEARBY_PREFIX = "/b2b-nearby/"
RELAY_PREFIX = "/b2b-relay/"
BOOTH_PREFIX = "/b2b-booth/"
#: A booth envelope (a resend or snapshot answer is in the reply body, not here) is at most this.
MAX_BOOTH_BODY = 256 * 1024


class PairingHost(Protocol):
    """What the door asks of the engine-side pairing service."""

    device_id: str
    label: str
    #: The nearby service (``b2b_nearby.NearbyService``) or None while it is not running.
    nearby: Any
    #: B2BRELAY-01: the Link relay (``link_relay.LinkRelay``) or None while it is not running.
    relay: Any
    #: B2BBOOTH-91: the shared booth (``apps.b2b_booth.booth.Booth``) or None while none is attached.
    booth: Any

    def paired_token(self) -> str | None: ...
    def invite_open(self) -> bool: ...
    def pair_start(self, body: dict[str, Any], source_ip: str) -> dict[str, Any]: ...
    def pair_finish(self, body: dict[str, Any]) -> dict[str, Any]: ...


Answer = tuple[int, bytes | dict[str, Any], str]
_JSON = "application/json"


_NOT_PAIRED = b'{"detail":"not paired, or wrong pairing token"}'
_NOT_SERVED = b'{"detail":"not served on the B2B door"}'


def answer_unpaired_get(host: PairingHost, path: str) -> Answer | None:
    """The token-free GETs (hello, nearby), or None for a paired read."""
    if path == HELLO:
        hello = {"v": PROTOCOL_VERSION, "device_id": host.device_id, "label": host.label, "invite_open": host.invite_open()}
        return 200, hello, _JSON
    if path.startswith(NEARBY_PREFIX):
        return _nearby(host, lambda nearby: nearby.door_get(path))
    return None


def admit_paired_read(host: PairingHost, raw_path: str, method: str, authorization: str) -> str | Answer:
    """The engine path to forward for a paired read, or the refusal. Same allowlist and order
    as ``peer_listener`` (404, 405, 401, 422); the token is read per request."""
    token = host.paired_token()
    expected = f"Bearer {token}".encode() if token else None
    admitted = admit_peer_read(raw_path, method, authorization, expected, _NOT_PAIRED)
    if isinstance(admitted, str):
        return admitted
    status, body = admitted
    return status, (_NOT_SERVED if status == 404 else body), _JSON


#: Paths the buffered answer below never serves: virtual USB bodies can be gigabytes, and
#: only the LAN door streams them (``_serve_paired_read``).
_STREAM_ONLY_PREFIX = "/api/v1/b2b/usb/"
#: The engine read timeout for a buffered paired read.
BUFFERED_READ_TIMEOUT_S = 3.0


def answer_get(host: PairingHost, engine_url: str, path: str, query: str, authorization: str) -> Answer:
    """One whole GET answer, for a transport that carries a buffered body (the sealed internet
    door, ``b2b_transport.door``): the same admission as the LAN door, minus the streamed
    virtual USB routes, which are 404 here."""
    unpaired = answer_unpaired_get(host, path)
    if unpaired is not None:
        return unpaired
    if path.startswith(_STREAM_ONLY_PREFIX):
        return 404, _NOT_SERVED, _JSON
    admitted = admit_paired_read(host, path + (f"?{query}" if query else ""), "GET", authorization)
    if not isinstance(admitted, str):
        return admitted
    try:
        upstream = httpx.get(f"{engine_url}{admitted}", timeout=BUFFERED_READ_TIMEOUT_S)
    except httpx.HTTPError as exc:
        return 502, {"detail": f"engine unreachable: {type(exc).__name__}"}, _JSON
    return upstream.status_code, upstream.content, upstream.headers.get("content-type", _JSON)


def _nearby(host: PairingHost, call: Callable[[Any], dict[str, Any]]) -> Answer:
    nearby = host.nearby
    if nearby is None:
        return 404, {"detail": {"code": "nearby_off", "message": "this laptop is not listed nearby"}}, _JSON
    try:
        return 200, call(nearby), _JSON
    except PairingError as exc:
        return exc.status, {"detail": {"code": exc.code, "message": str(exc)}}, _JSON
    except (ValueError, KeyError, TypeError) as exc:
        return 400, {"detail": {"code": "bad_message", "message": str(exc)}}, _JSON


def answer_relay(host: PairingHost, path: str, raw: bytes) -> Answer:
    relay = host.relay
    if relay is None:
        return 404, {"detail": {"code": "relay_off", "message": "the Link relay is not running here"}}, _JSON
    status, body = relay.door_post(path, raw)
    return status, body, _JSON


def answer_booth(host: PairingHost, path: str, raw: bytes, authorization: str) -> Answer:
    """A booth lane POST from the paired partner: the pairing token is required (constant-time),
    then the booth answers. The internet door supplies the token itself (its session is the proof)."""
    token = host.paired_token()
    expected = f"Bearer {token}".encode() if token else None
    if expected is None or not hmac.compare_digest((authorization or "").encode(), expected):
        return 401, _NOT_PAIRED, _JSON
    booth = getattr(host, "booth", None)
    if booth is None:
        return 404, {"detail": {"code": "booth_off", "message": "no shared booth is attached on this laptop"}}, _JSON
    status, body = booth.door_post(path[len(BOOTH_PREFIX):], raw)
    return status, body, _JSON


def answer_post(host: PairingHost, path: str, raw: bytes, source_ip: str, local: str | None = None) -> Answer:
    """``local`` is this door as the caller dialed it (``host:port``), which only a LAN socket
    knows; the nearby Reconnect checks its proof against it (B2BREADDR-03)."""
    if path.startswith(NEARBY_PREFIX):
        try:
            body = json.loads(raw or b"{}")
        except ValueError as exc:
            return 400, {"detail": {"code": "bad_message", "message": str(exc)}}, _JSON
        if not isinstance(body, dict):
            return 400, {"detail": {"code": "bad_message", "message": "body must be a JSON object"}}, _JSON
        return _nearby(host, lambda nearby: nearby.door_post(path, body, source_ip, local))
    if path not in (START, FINISH):
        return 404, {"detail": "not served on the B2B door"}, _JSON
    try:
        body = json.loads(raw or b"{}")
        if not isinstance(body, dict):
            return 400, {"detail": {"code": "bad_message", "message": "body must be a JSON object"}}, _JSON
        answer = host.pair_start(body, source_ip) if path == START else host.pair_finish(body)
    except PairingError as exc:
        return exc.status, {"detail": {"code": exc.code, "message": str(exc)}}, _JSON
    except (ValueError, KeyError, TypeError) as exc:
        return 400, {"detail": {"code": "bad_message", "message": str(exc)}}, _JSON
    return 200, answer, _JSON


def _serve_paired_read(
    handler: BaseHTTPRequestHandler, send: Callable[[Answer], None], host: PairingHost, engine_url: str, method: str
) -> None:
    """Admit a paired read, then stream the engine's answer (Range forwarded) or send the refusal."""
    admitted = admit_paired_read(host, handler.path, method, handler.headers.get("Authorization") or "")
    if not isinstance(admitted, str):
        send(admitted)
        return
    # B2BAUDIO-23: a relayed answer with a Content-Length that arrives whole leaves the partner's
    # connection open (the partner polls once a second; a new handshake each time was the cost);
    # one streamed without a length, or cut short, closes it, and says so (`relay_engine_response`).
    headers = {"Range": handler.headers["Range"]} if handler.headers.get("Range") else {}
    try:
        relay_engine_response(handler, method, f"{engine_url}{admitted}", headers)
    except httpx.HTTPError as exc:
        send((502, {"detail": f"engine unreachable: {type(exc).__name__}"}, _JSON))


def _serve_get(handler: BaseHTTPRequestHandler, send: Callable[[Answer], None], host: PairingHost, engine_url: str) -> None:
    """A token-free GET (hello, nearby) answers directly; anything else is a paired read."""
    unpaired = answer_unpaired_get(host, urlsplit(handler.path).path)
    if unpaired is not None:
        send(unpaired)
    else:
        _serve_paired_read(handler, send, host, engine_url, "GET")


def _handler(host: PairingHost, engine_url: Callable[[], str]) -> type[BaseHTTPRequestHandler]:
    class DoorHandler(BaseHTTPRequestHandler):
        server_version = "opendj-b2b-door/1"
        protocol_version = "HTTP/1.1"
        disable_nagle_algorithm = True
        #: An idle keep-alive connection is closed after this long (seconds), so threads do not pile up.
        timeout = 30.0

        def _send(self, answer: Answer) -> None:
            status, body, content_type = answer
            data = json.dumps(body).encode() if isinstance(body, dict) else body
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def do_GET(self) -> None:
            _serve_get(self, self._send, host, engine_url())

        def do_HEAD(self) -> None:
            _serve_paired_read(self, self._send, host, engine_url(), "HEAD")

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            path = urlsplit(self.path).path
            if length > (MAX_BOOTH_BODY if path.startswith(BOOTH_PREFIX) else MAX_BODY):
                self.close_connection = True  # the unread body must not be parsed as the next request
                self._send((413, {"detail": "body too large"}, _JSON))
                return
            raw = self.rfile.read(length)
            if path.startswith(BOOTH_PREFIX):
                self._send(answer_booth(host, path, raw, self.headers.get("Authorization") or ""))
                return
            if path.startswith(RELAY_PREFIX):
                self._send(answer_relay(host, path, raw))
                return
            dialed_host, dialed_port = self.connection.getsockname()[:2]
            self._send(answer_post(host, path, raw, self.client_address[0], f"{dialed_host}:{dialed_port}"))

        def do_PUT(self) -> None:
            self.close_connection = True  # any body is unread
            self._send((405, {"detail": "GET, HEAD and the pairing POSTs only"}, _JSON))

        do_DELETE = do_PATCH = do_PUT

        def log_message(self, format: str, *args: object) -> None:
            relay_ok = getattr(self, "path", "").startswith(RELAY_PREFIX) and len(args) > 1 and str(args[1]) == "200"
            if relay_ok:
                return  # the relay's 4 Hz pings and long polls would bury every other line
            sys.stderr.write(f"b2b_door {self.address_string()} {format % args}\n")

    return DoorHandler


class _DoorServer(ThreadingHTTPServer):
    """Tracks open connections: with keep-alive a closed door must also hang up on the partner's
    open connections, or their threads keep answering until the idle timeout."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler]) -> None:
        super().__init__(address, handler)
        self.open_connections: set[socket.socket] = set()
        self.connections_lock = threading.Lock()

    def process_request(self, request: Any, client_address: Any) -> None:
        with self.connections_lock:
            self.open_connections.add(request)
        super().process_request(request, client_address)

    def shutdown_request(self, request: Any) -> None:
        with self.connections_lock:
            self.open_connections.discard(request)
        super().shutdown_request(request)

    def hang_up_all(self) -> None:
        with self.connections_lock:
            connections = list(self.open_connections)
        for connection in connections:
            with contextlib.suppress(OSError):  # already closed by its own thread
                connection.shutdown(socket.SHUT_RDWR)


class Door:
    """The LAN server thread. ``port`` 0 lets the OS pick; the bound port is ``self.port``."""

    def __init__(self, host: PairingHost, engine_url: Callable[[], str], port: int, bind: str = "0.0.0.0") -> None:
        self._server = _DoorServer((bind, port), _handler(host, engine_url))
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, name="b2b-door", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.hang_up_all()
        self._server.server_close()
        self._thread.join(timeout=2)
