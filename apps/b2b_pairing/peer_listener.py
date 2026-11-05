"""LINK-25 + VUSB-03: opt-in, token-guarded, read-only LAN door to a few engine routes.

The engine stays bound to 127.0.0.1. For a B2B set without an SSH tunnel, run this
beside the engine on the laptop whose decks (and lent playlists) the OTHER laptop should see:

    MDT_B2B_PEER_TOKEN=<shared secret, 16+ chars> \\
        uv run python -m apps.b2b_pairing.peer_listener --engine http://127.0.0.1:8702 --port 17710

It listens on ``--host`` (default 0.0.0.0) and forwards only this allowlist to the loopback
engine, with ``Authorization: Bearer <token>``:

- ``GET  /api/v1/b2b/now-playing[?with_overview=0|1]``           PEER decks (LINK-24)
- ``GET  /api/v1/b2b/shouts/outbox[?since=<seq>]``                shout bubbles (LINK-29..31)
- ``GET  /api/v1/b2b/cursor/latest``                             peer cursor (B2BCURSOR-01..03)
- ``GET  /api/v1/b2b/partner-audio/outbox[?since=<seq>]``        partner-audio signalling (B2BAUDIO-02)
- ``GET  /api/v1/b2b/multicast/self``                            this laptop's Link and Nearby state (B2BMCAST-03)
- ``GET  /api/v1/b2b/now-playing/artwork/<deck 1-4>``          the cover of a LOADED deck only, never a library lookup (B2BDECKS-02)
- ``GET  /api/v1/b2b/usb/export[?summary=0|1]``                  virtual USB manifest (VUSB-02)
- ``GET|HEAD /api/v1/b2b/usb/export/tracks/{id}/audio``          a lent track's audio, Range forwarded
- ``GET  /api/v1/b2b/usb/export/tracks/{id}/anlz[?points=N]``    a lent track's analysis

The engine itself refuses any track outside a lent playlist. Any other path is 404, any
other method 405, a missing or wrong token 401 (constant-time compare), and it refuses to
start without a token. Bodies stream through in chunks; it holds no engine state and
cannot write anything.
"""

from __future__ import annotations

import argparse
import hmac
import os
import re
import sys
import threading
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from apps.shared.link_constants import CLAIM_PARAM_RE

TOKEN_ENV = "MDT_B2B_PEER_TOKEN"
MIN_TOKEN_LEN = 16
FORWARD_TIMEOUT_S = httpx.Timeout(3.0, read=30.0)
STREAM_CHUNK = 1 << 16
SOCKET_TIMEOUT_S = 10.0
_ID = r"[A-Za-z0-9_-]{1,128}"
MULTICAST_SELF_ROUTE = "/api/v1/b2b/multicast/self"
CURSOR_ROUTE = "/api/v1/b2b/cursor/latest"
#: B2BAUDIO-02: this laptop's partner-audio signalling outbox (offer, answer, ICE), read by the partner.
PARTNER_AUDIO_OUTBOX_ROUTE = "/api/v1/b2b/partner-audio/outbox"
#: path regex -> (allowed methods, {query param: value regex}).
ALLOWED: tuple[tuple[re.Pattern[str], frozenset[str], dict[str, str]], ...] = (
    #: LINKROLE-01: ``link_claim`` is the reading engine's take-over claim; it can only make this
    #: engine FOLLOW (a later take-over by the reader), never take ON AIR.
    (re.compile(r"^/api/v1/b2b/now-playing$"), frozenset({"GET"}), {"with_overview": r"^[01]$", "link_claim": rf"^{CLAIM_PARAM_RE}$"}),
    #: B2BDECKS-02: the artwork of whatever is loaded on deck 1-4; the path carries no track id.
    (re.compile(r"^/api/v1/b2b/now-playing/artwork/[1-4]$"), frozenset({"GET"}), {}),
    (re.compile(r"^/api/v1/b2b/shouts/outbox$"), frozenset({"GET"}), {"since": r"^[0-9]{1,18}$"}),
    #: B2BMCAST-03: the partner's own Link and Nearby state, so a multicast diagnosis can tell
    #: "the network blocks Link" from "Link is off over there". Read-only, no query.
    (re.compile(rf"^{re.escape(MULTICAST_SELF_ROUTE)}$"), frozenset({"GET"}), {}),
    #: B2BCURSOR-01..03: the partner's latest cursor slot. Read-only, no query.
    (re.compile(rf"^{re.escape(CURSOR_ROUTE)}$"), frozenset({"GET"}), {}),
    #: B2BAUDIO-02: the partner-audio signalling outbox. Read-only; only ``since`` is accepted.
    (re.compile(rf"^{re.escape(PARTNER_AUDIO_OUTBOX_ROUTE)}$"), frozenset({"GET"}), {"since": r"^[0-9]{1,18}$"}),
    (re.compile(r"^/api/v1/b2b/usb/export$"), frozenset({"GET"}), {"summary": r"^[01]$"}),
    (re.compile(rf"^/api/v1/b2b/usb/export/tracks/{_ID}/audio$"), frozenset({"GET", "HEAD"}), {}),
    (re.compile(rf"^/api/v1/b2b/usb/export/tracks/{_ID}/anlz$"), frozenset({"GET"}), {"points": r"^[0-9]{1,6}$"}),
)
_NOT_ALLOWLISTED = (
    b'{"detail":"only the B2B now-playing, deck artwork, shout outbox, cursor, multicast self'
    b', partner-audio outbox and virtual USB export routes are served here"}'
)
_UNAUTHORIZED = b'{"detail":"missing or wrong bearer token (MDT_B2B_PEER_TOKEN)"}'
_FORWARDED_HEADERS = ("content-type", "content-length", "content-range", "accept-ranges", "etag", "cache-control")


def _match(path: str) -> tuple[frozenset[str], dict[str, str]] | None:
    for pattern, methods, params in ALLOWED:
        if pattern.fullmatch(path):
            return methods, params
    return None


def _validated_query(raw: str, params: dict[str, str]) -> str | None:
    """The query re-encoded from allowlisted, well-formed parameters only; None refuses it."""
    out: dict[str, str] = {}
    for name, values in parse_qs(raw, keep_blank_values=True).items():
        rule = params.get(name)
        if rule is None or len(values) != 1 or not re.fullmatch(rule, values[0]):
            return None
        out[name] = values[0]
    return urlencode(out)


def admit_peer_read(
    raw_path: str, method: str, authorization: str | None, expected: bytes | None, unauthorized: bytes
) -> str | tuple[int, bytes]:
    """The engine path + query to forward, or the refusal ``(status, body)``. Order: unknown
    path 404, wrong method 405, bad token 401 (body ``unauthorized``; ``expected`` None means
    no token exists, which always refuses), bad query 422. Shared with ``b2b_door``."""
    parts = urlsplit(raw_path)
    allowed = _match(parts.path)
    if allowed is None:
        return 404, _NOT_ALLOWLISTED
    methods, params = allowed
    if method not in methods:
        return 405, b""
    if expected is None or not hmac.compare_digest((authorization or "").encode(), expected):
        return 401, unauthorized
    query = _validated_query(parts.query, params)
    if query is None:
        return 422, b'{"detail":"unexpected or malformed query parameter"}'
    return parts.path + (f"?{query}" if query else "")


_ENGINE_CLIENT_LOCK = threading.Lock()
#: The kept-alive client for relays to this laptop's own engine: empty until the first, then one.
_engine_client: list[httpx.Client] = []


def _kept_engine_client() -> httpx.Client:
    """The one client every relayed read uses to reach this laptop's engine (B2BAUDIO-23).

    ``httpx.stream`` at module level builds a client for each call (its TLS context included,
    tens of ms of CPU) and opens a new connection, inside the door's thread of a busy engine.
    The partner polls this about once a second: one warm connection serves it."""
    with _ENGINE_CLIENT_LOCK:
        if not _engine_client:
            _engine_client.append(httpx.Client(timeout=FORWARD_TIMEOUT_S, limits=httpx.Limits(max_keepalive_connections=8, keepalive_expiry=25.0)))
        return _engine_client[0]


def close_engine_client() -> None:
    """Drop the kept connections to the engine (tests)."""
    with _ENGINE_CLIENT_LOCK:
        for client in _engine_client:
            client.close()
        _engine_client.clear()


def relay_engine_response(handler: BaseHTTPRequestHandler, method: str, target: str, headers: dict[str, str]) -> None:
    """Stream the engine's answer back: status, the allowlisted headers, then the body.

    The partner's connection is left as the handler has it (kept, on a keep-alive door) only
    when the answer is delimited by a Content-Length and arrived whole; otherwise the partner is
    told ``Connection: close`` and the connection ends with the body. A kept connection to the
    engine that the engine has since closed is tried once more on a new one (a GET or HEAD)."""
    client = _kept_engine_client()
    answered = False
    for attempt in (0, 1):
        try:
            with client.stream(method, target, headers=headers) as upstream:
                answered = True
                delimited = "content-length" in upstream.headers
                handler.send_response(upstream.status_code)
                for name in _FORWARDED_HEADERS:
                    if name in upstream.headers:
                        handler.send_header(name, upstream.headers[name])
                if not delimited:
                    handler.send_header("Connection", "close")
                    handler.close_connection = True
                handler.end_headers()
                if method != "HEAD":
                    try:
                        for chunk in upstream.iter_raw(STREAM_CHUNK):
                            handler.wfile.write(chunk)
                    except httpx.HTTPError as exc:
                        # Headers are already sent: the client sees a short body against its
                        # Content-Length (the borrower resumes from its .part), never a 502 tail.
                        handler.close_connection = True
                        sys.stderr.write(f"peer_listener: engine stream broke mid-body: {type(exc).__name__}\n")
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError):
            # A kept connection the engine had closed: once more on a new one, but only while
            # nothing has been sent to the partner.
            if answered or attempt == 1:
                raise
        else:
            return


def make_handler(engine_url: str, token: str) -> type[BaseHTTPRequestHandler]:
    expected = f"Bearer {token}".encode()

    class PeerHandler(BaseHTTPRequestHandler):
        server_version = "opendj-b2b-peer/2"
        #: A silent or slow client (pre-auth slowloris) is dropped instead of pinning a thread.
        timeout = SOCKET_TIMEOUT_S

        def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _refuse_method(self) -> None:
            self._send(405, b'{"detail":"read-only: GET and HEAD only"}')

        do_POST = do_PUT = do_PATCH = do_DELETE = _refuse_method

        def do_HEAD(self) -> None:
            self._forward("HEAD")

        def do_GET(self) -> None:
            self._forward("GET")

        def _forward(self, method: str) -> None:
            admitted = admit_peer_read(self.path, method, self.headers.get("Authorization"), expected, _UNAUTHORIZED)
            if isinstance(admitted, tuple):
                if admitted[0] == 405:
                    self._refuse_method()
                else:
                    self._send(*admitted)
                return
            headers = {"Range": self.headers["Range"]} if self.headers.get("Range") else {}
            try:
                relay_engine_response(self, method, f"{engine_url}{admitted}", headers)
            except httpx.HTTPError as exc:
                self._send(502, f'{{"detail":"engine unreachable: {type(exc).__name__}"}}'.encode())

        def log_message(self, format: str, *args: object) -> None:
            sys.stderr.write(f"peer_listener {self.address_string()} {format % args}\n")

    return PeerHandler


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m apps.b2b_pairing.peer_listener", description=__doc__.splitlines()[0])
    parser.add_argument("--engine", required=True, help="this laptop's engine, e.g. http://127.0.0.1:8702")
    parser.add_argument("--host", default="0.0.0.0", help="bind address (default 0.0.0.0, the LAN)")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    token = os.environ.get(TOKEN_ENV, "")
    if len(token) < MIN_TOKEN_LEN:
        print(f"[ERROR] {TOKEN_ENV} must be set to a shared secret of {MIN_TOKEN_LEN}+ characters", file=sys.stderr)
        return 2
    engine_url = args.engine.rstrip("/")
    if not engine_url.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")):
        print(f"[ERROR] --engine must be a loopback URL, got {engine_url}", file=sys.stderr)
        return 2
    server = ThreadingHTTPServer((args.host, args.port), make_handler(engine_url, token))
    server.daemon_threads = True
    print(f"[OK] peer listener on {args.host}:{args.port} -> {engine_url} ({len(ALLOWED)} read-only routes, token required)", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
