"""Pure-ASGI HTTP guards and 2 MiB deck-audio chunks (perf: audio-serving).

[if] a request the five old BaseHTTPMiddleware guards rejected now passes, or one they let through is now rejected [then] broken, [else stop].
[if] a Range or If-Range request on deck audio returns different bytes with 2 MiB chunks [then] broken, [else stop].
[if] a client disconnect mid-stream leaves the audio file or a pinned stick descriptor open [then] broken, [else stop].

``HttpGuardMiddleware`` replaced five ``@app.middleware("http")`` layers (bind
warning, passive usage, share gate, origin guard, host allowlist, outermost
first). The differential test rebuilds that legacy stack around the SAME
create_app engine, in the exact decorator form app_wiring.py used before the
change, and asserts every response in a request matrix (guards, CORS
preflight, deck audio with Range/HEAD/416) is byte-identical, plus the same
passive-usage calls. The explicit tests then pin each rejection on the
streaming audio route by status and code, with an allowed request as the
control, so the differential cannot pass by both stacks being wrong.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psutil
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from apps.adapters.rekordbox import config as rb_config
from apps.shared.fd_anchored_walk import FD_ANCHORED_WALK_SUPPORTED
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.http_guards import HttpGuardMiddleware
from apps.webui.server.request_guard import host_allowlist_rejection, origin_guard_rejection
from apps.webui.server.routes.rb_assets_audio import AUDIO_CHUNK_SIZE_BYTES, AudioFileResponse
from apps.webui.server.share_gate import AUTH_TOKEN, ShareConfig, share_gate_rejection
from tests.webui.test_audio_deck_load_contract import _insert_track

pytestmark = [
    pytest.mark.requirement("SEC-01"),
    pytest.mark.requirement("CAT-05"),
    pytest.mark.requirement("AUDIO-ACCESS-01"),
]

CHUNK = AUDIO_CHUNK_SIZE_BYTES
SID = "c" * 40
AUDIO_URL = f"/api/v1/tracks/{SID}/audio"
SHARE_HOST = "dj.example"
TOKEN = "share-secret-token"
FRONTEND_PORT = 9411
BACKEND_PORT = 8697
FRONTEND_ORIGIN = f"http://127.0.0.1:{FRONTEND_PORT}"
# Two full chunks plus an odd tail, so every chunk-boundary case is reachable.
AUDIO_SIZE = 2 * CHUNK + 12_345
# Load-proof probe deadline, as in test_audio_deck_load_contract.py.
PROBE_TIMEOUT_S = 30.0


def _audio_bytes() -> bytes:
    pattern = bytes(range(256)) * ((AUDIO_SIZE // 256) + 1)
    return (b"ID3" + pattern)[:AUDIO_SIZE]


AUDIO = _audio_bytes()


@pytest.fixture
def audio_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "library" / "deck.mp3"
    path.parent.mkdir()
    path.write_bytes(AUDIO)
    state_path = tmp_path / "state.db"
    _insert_track(state_path, SID, str(path))
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", tmp_path / "absent-master.db")
    return path


def _app(bind_host: str = "127.0.0.1") -> FastAPI:
    return create_app(
        backend=InMemoryBackend(),
        mount_frontend=False,
        enable_cors=True,
        port=BACKEND_PORT,
        frontend_port=FRONTEND_PORT,
        bind_host=bind_host,
        hostname="test-host",
        lock_status_fn=lambda: None,
        syncthing_status_fn=lambda: None,
        audio_access_timeout_s=PROBE_TIMEOUT_S,
        share_config=ShareConfig(host=SHARE_HOST, auth=AUTH_TOKEN, token=TOKEN, read_only=True),
    )


def _install_legacy_guard_stack(app: FastAPI, bind_host: str) -> None:
    """Swap HttpGuardMiddleware for the five pre-change BaseHTTPMiddleware layers.

    Each dispatcher is the pre-change function body: ``rejection or call_next``.
    Order is the pre-change registration order (host, origin, share, usage,
    bind warning), which Starlette's insert-at-0 makes bind warning outermost.
    """

    async def host_allowlist(request: Request, call_next: Any) -> Response:
        rejection = host_allowlist_rejection(request)
        return rejection if rejection is not None else await call_next(request)

    async def origin_guard(request: Request, call_next: Any) -> Response:
        rejection = origin_guard_rejection(request)
        return rejection if rejection is not None else await call_next(request)

    async def share_gate(request: Request, call_next: Any) -> Response:
        rejection = share_gate_rejection(request)
        return rejection if rejection is not None else await call_next(request)

    async def record_passive_usage(request: Request, call_next: Any) -> Response:
        app.state.usage_store.record_request(
            path=request.url.path,
            user_agent=request.headers.get("user-agent", ""),
        )
        return await call_next(request)

    async def add_bind_warning(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        if bind_host and bind_host not in {"127.0.0.1", "localhost"}:
            response.headers["X-Bind-Warning"] = f"server is bound to {bind_host}; do not expose without Tailscale"
        return response

    positions = [i for i, m in enumerate(app.user_middleware) if m.cls is HttpGuardMiddleware]
    assert len(positions) == 1, app.user_middleware
    legacy = [
        Middleware(BaseHTTPMiddleware, dispatch=fn)
        for fn in (add_bind_warning, record_passive_usage, share_gate, origin_guard, host_allowlist)
    ]
    app.user_middleware[positions[0] : positions[0] + 1] = legacy
    assert app.middleware_stack is None, "middleware stack already built; the swap would be ignored"


def _spy_usage(app: FastAPI) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    original = app.state.usage_store.record_request

    def record_request(*, path: str, user_agent: str) -> Any:
        calls.append((path, user_agent))
        return original(path=path, user_agent=user_agent)

    app.state.usage_store.record_request = record_request
    return calls


BEARER = {"Authorization": f"Bearer {TOKEN}"}
TRACK_PATCH = f"/api/v1/tracks/{'a' * 40}"
PLAY = {"single": {"type": "play", "deck": 1, "playing": True}}

# (method, path, headers, json) -- every guard's reject and allow side, on a
# JSON route and on the streaming audio route.
MATRIX: list[tuple[str, str, dict[str, str], Any]] = [
    ("GET", "/api/v1/health", {}, None),
    ("GET", "/api/v1/health", {"Host": "evil.example"}, None),
    ("GET", "/api/v1/health", {"Host": SHARE_HOST}, None),
    ("GET", "/api/v1/settings", {"Host": SHARE_HOST}, None),
    ("GET", "/api/v1/settings", {"Host": SHARE_HOST, **BEARER}, None),
    ("GET", "/api/v1/settings", {"Host": SHARE_HOST, "Authorization": "Bearer wrong"}, None),
    ("PATCH", TRACK_PATCH, {"Host": SHARE_HOST, **BEARER}, {"rating": 5}),
    ("POST", "/api/v1/commands", {"Origin": "https://evil.example"}, PLAY),
    ("POST", "/api/v1/commands", {"Origin": FRONTEND_ORIGIN}, PLAY),
    ("POST", "/api/v1/commands", {}, PLAY),
    ("POST", "/api/v1/commands", {"Host": "evil.example", "Origin": "https://evil.example"}, PLAY),
    (
        "OPTIONS",
        "/api/v1/commands",
        {"Origin": FRONTEND_ORIGIN, "Access-Control-Request-Method": "POST"},
        None,
    ),
    (
        "OPTIONS",
        "/api/v1/commands",
        {"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
        None,
    ),
    ("GET", "/api/v1/no-such-route", {}, None),
    ("GET", AUDIO_URL, {}, None),
    ("GET", AUDIO_URL, {"Range": "bytes=0-99"}, None),
    ("GET", AUDIO_URL, {"Range": f"bytes={CHUNK - 5}-{CHUNK + 4}"}, None),
    ("GET", AUDIO_URL, {"Range": f"bytes={AUDIO_SIZE}-"}, None),
    ("GET", AUDIO_URL, {"Range": "garbage"}, None),
    ("HEAD", AUDIO_URL, {}, None),
    ("GET", AUDIO_URL, {"Host": "evil.example", "Range": "bytes=0-99"}, None),
    ("GET", AUDIO_URL, {"Host": SHARE_HOST, "Range": "bytes=0-99"}, None),
    ("GET", AUDIO_URL, {"Host": SHARE_HOST, "Range": "bytes=0-99", **BEARER}, None),
    ("GET", AUDIO_URL, {"Origin": "https://evil.example", "Range": "bytes=0-99"}, None),
]


def _run_matrix(app: FastAPI) -> list[tuple[int, bytes, list[tuple[str, str]]]]:
    out = []
    with TestClient(app, raise_server_exceptions=False, base_url="http://127.0.0.1") as client:
        for method, path, headers, body in MATRIX:
            response = client.request(method, path, headers=headers, json=body)
            out.append((response.status_code, response.content, sorted(response.headers.multi_items())))
    return out


@pytest.mark.parametrize("bind_host", ["127.0.0.1", "0.0.0.0"])
def test_pure_asgi_guard_answers_every_request_exactly_like_the_legacy_stack(audio_file: Path, bind_host: str) -> None:
    """[if] any matrix row differs in status, bytes or headers from the legacy five-layer stack [then] the guard changed behavior, [else stop]."""
    legacy_app = _app(bind_host)
    _install_legacy_guard_stack(legacy_app, bind_host)
    legacy_usage = _spy_usage(legacy_app)
    legacy = _run_matrix(legacy_app)

    new_app = _app(bind_host)
    new_usage = _spy_usage(new_app)
    new = _run_matrix(new_app)

    for row, (want, got) in enumerate(zip(legacy, new, strict=True)):
        assert got == want, f"row {row} {MATRIX[row][:3]} differs"
    assert new_usage == legacy_usage
    # Passive usage sees every http request, the guard-rejected ones included.
    assert len(new_usage) == len(MATRIX)
    # The matrix really exercised both sides of each guard (not all-pass, not all-fail).
    statuses = [status for status, _body, _headers in new]
    assert {200, 206, 401, 403, 416} <= set(statuses), statuses


def _client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False, base_url="http://127.0.0.1")


def test_guards_reject_on_the_streaming_audio_route(audio_file: Path) -> None:
    """[if] a foreign Host or a tokenless share-host Range request reaches the deck audio bytes [then] broken, [else stop]."""
    with _client(_app()) as client:
        foreign = client.get(AUDIO_URL, headers={"Host": "evil.example", "Range": "bytes=0-99"})
        assert foreign.status_code == 403
        assert foreign.json()["code"] == "HOST_NOT_ALLOWED"

        tokenless = client.get(AUDIO_URL, headers={"Host": SHARE_HOST, "Range": "bytes=0-99"})
        assert tokenless.status_code == 401
        assert tokenless.json()["code"] == "SHARE_UNAUTHORIZED"

        wrong = client.head(AUDIO_URL, headers={"Host": SHARE_HOST, "Authorization": "Bearer wrong"})
        assert wrong.status_code == 401

        # Control: the same request from loopback streams the bytes.
        allowed = client.get(AUDIO_URL, headers={"Range": "bytes=0-99"})
        assert allowed.status_code == 206
        assert allowed.content == AUDIO[:100]

        # Control: a share-host caller with the token is let through to the route.
        shared = client.get(AUDIO_URL, headers={"Host": SHARE_HOST, "Range": "bytes=0-99", **BEARER})
        assert shared.status_code == 206
        assert shared.content == AUDIO[:100]

        # Control against over-rejection: the origin guard covers mutating
        # methods only, so a cross-origin deck-audio GET still streams.
        cross_origin = client.get(AUDIO_URL, headers={"Origin": "https://evil.example", "Range": "bytes=0-99"})
        assert cross_origin.status_code == 206
        assert cross_origin.content == AUDIO[:100]


def test_share_gate_still_blocks_writes_and_foreign_origins(audio_file: Path) -> None:
    """[if] the read-only share gate or the origin guard lets a mutation through [then] broken, [else stop]."""
    with _client(_app()) as client:
        read_only = client.patch(TRACK_PATCH, headers={"Host": SHARE_HOST, **BEARER}, json={"rating": 5})
        assert read_only.status_code == 403
        assert read_only.json()["code"] == "SHARE_READ_ONLY"

        foreign_origin = client.post("/api/v1/commands", headers={"Origin": "https://evil.example"}, json=PLAY)
        assert foreign_origin.status_code == 403
        assert foreign_origin.json()["code"] == "ORIGIN_NOT_ALLOWED"

        # Control: the frontend origin is not rejected by any guard.
        trusted = client.post("/api/v1/commands", headers={"Origin": FRONTEND_ORIGIN}, json=PLAY)
        assert trusted.status_code != 403 or trusted.json().get("code") not in {
            "ORIGIN_NOT_ALLOWED",
            "HOST_NOT_ALLOWED",
            "SHARE_READ_ONLY",
        }


def test_bind_warning_rides_streamed_206_and_rejections_only_off_loopback(audio_file: Path) -> None:
    """[if] X-Bind-Warning is missing from a streamed 206 or a 403 on a public bind, or present on loopback [then] broken, [else stop]."""
    with _client(_app("0.0.0.0")) as client:
        ranged = client.get(AUDIO_URL, headers={"Range": f"bytes={CHUNK - 5}-{CHUNK + 4}"})
        assert ranged.status_code == 206
        assert "0.0.0.0" in ranged.headers["X-Bind-Warning"]
        rejected = client.get(AUDIO_URL, headers={"Host": "evil.example"})
        assert rejected.status_code == 403
        assert "X-Bind-Warning" in rejected.headers
    with _client(_app("127.0.0.1")) as client:
        ranged = client.get(AUDIO_URL, headers={"Range": "bytes=0-0"})
        assert ranged.status_code == 206
        assert "X-Bind-Warning" not in ranged.headers


def test_websocket_and_lifespan_scopes_bypass_the_guard() -> None:
    """[if] a non-http scope is run through the guards [then] lifespan breaks on startup, [else stop]."""
    seen: list[str] = []

    async def inner(scope: dict[str, Any], receive: Any, send: Any) -> None:
        seen.append(scope["type"])

    guard = HttpGuardMiddleware(inner, bind_host="0.0.0.0")

    async def noop(*_args: Any) -> None:
        return None

    async def run() -> None:
        await guard({"type": "lifespan"}, noop, noop)
        await guard({"type": "websocket", "path": "/ws", "headers": []}, noop, noop)

    asyncio.run(run())
    assert seen == ["lifespan", "websocket"]


# -- Range / If-Range bytes with 2 MiB chunks ---------------------------------


@pytest.mark.parametrize(
    ("range_header", "start", "end"),
    [
        ("bytes=0-0", 0, 1),
        ("bytes=0-99", 0, 100),
        (f"bytes={CHUNK - 10}-{CHUNK + 9}", CHUNK - 10, CHUNK + 10),
        (f"bytes={CHUNK}-{2 * CHUNK - 1}", CHUNK, 2 * CHUNK),
        (f"bytes={CHUNK - 1}-{2 * CHUNK}", CHUNK - 1, 2 * CHUNK + 1),
        ("bytes=100-", 100, AUDIO_SIZE),
        ("bytes=-100", AUDIO_SIZE - 100, AUDIO_SIZE),
        (f"bytes=0-{AUDIO_SIZE - 1}", 0, AUDIO_SIZE),
        (f"bytes=0-{AUDIO_SIZE + 999}", 0, AUDIO_SIZE),
    ],
)
def test_range_returns_exact_bytes_across_chunk_boundaries(
    audio_file: Path, range_header: str, start: int, end: int
) -> None:
    """[if] a Range request returns bytes other than file[start:end] [then] seeking plays the wrong audio, [else stop]."""
    with _client(_app()) as client:
        response = client.get(AUDIO_URL, headers={"Range": range_header})
    assert response.status_code == 206
    assert response.content == AUDIO[start:end]
    assert response.headers["content-range"] == f"bytes {start}-{end - 1}/{AUDIO_SIZE}"
    assert int(response.headers["content-length"]) == end - start


def test_full_get_head_multi_range_and_416(audio_file: Path) -> None:
    """[if] a plain GET, HEAD, multi-range or unsatisfiable range answers differently [then] broken, [else stop]."""
    with _client(_app()) as client:
        full = client.get(AUDIO_URL)
        assert full.status_code == 200
        assert full.content == AUDIO
        assert full.headers["accept-ranges"] == "bytes"

        head = client.head(AUDIO_URL)
        assert head.status_code == 200
        assert head.content == b""
        assert int(head.headers["content-length"]) == AUDIO_SIZE

        multi = client.get(AUDIO_URL, headers={"Range": f"bytes=0-9,{CHUNK}-{CHUNK + 9}"})
        assert multi.status_code == 206
        assert multi.headers["content-type"].startswith("multipart/byteranges")
        assert AUDIO[0:10] in multi.content
        assert AUDIO[CHUNK : CHUNK + 10] in multi.content

        unsatisfiable = client.get(AUDIO_URL, headers={"Range": f"bytes={AUDIO_SIZE}-"})
        assert unsatisfiable.status_code == 416
        assert unsatisfiable.headers["content-range"] == f"bytes */{AUDIO_SIZE}"


def test_if_range_serves_partial_only_for_the_current_validator(audio_file: Path) -> None:
    """[if] a stale If-Range validator gets a 206 slice, or a current one gets the full file [then] a deck splices two file versions, [else stop]."""
    span = f"bytes={CHUNK - 10}-{CHUNK + 9}"
    with _client(_app()) as client:
        head = client.head(AUDIO_URL)
        etag, last_modified = head.headers["etag"], head.headers["last-modified"]
        for validator in (etag, last_modified):
            current = client.get(AUDIO_URL, headers={"Range": span, "If-Range": validator})
            assert current.status_code == 206
            assert current.content == AUDIO[CHUNK - 10 : CHUNK + 10]
        stale = client.get(AUDIO_URL, headers={"Range": span, "If-Range": '"stale-etag"'})
        assert stale.status_code == 200
        assert stale.content == AUDIO


# -- chunking and disconnects, driven over raw ASGI ---------------------------


def _scope(path: str, headers: list[tuple[bytes, bytes]] | None = None) -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"127.0.0.1"), *(headers or [])],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", BACKEND_PORT),
        "extensions": {},
        "state": {},
    }


async def _receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


def _body_chunks(app: Any, path: str, headers: list[tuple[bytes, bytes]] | None = None) -> list[bytes]:
    chunks: list[bytes] = []

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            chunks.append(message["body"])

    asyncio.run(app(_scope(path, headers), _receive, send))
    return chunks


def test_deck_audio_streams_in_2_mib_chunks_through_the_whole_stack(audio_file: Path) -> None:
    """[if] /audio is sent in 64 KiB pieces again, or a middleware re-chunks it [then] the measured saving is gone, [else stop].

    Control: AUDIO_SIZE is 2 chunks plus a tail, so 2 MiB chunking predicts
    exactly [CHUNK, CHUNK, tail]; Starlette's 64 KiB default would give 81.
    """
    assert CHUNK == 2 * 1024 * 1024
    assert AudioFileResponse.chunk_size == CHUNK
    chunks = _body_chunks(_app("0.0.0.0"), AUDIO_URL)
    assert [len(c) for c in chunks] == [CHUNK, CHUNK, AUDIO_SIZE - 2 * CHUNK]
    assert b"".join(chunks) == AUDIO

    ranged = _body_chunks(_app("0.0.0.0"), AUDIO_URL, [(b"range", f"bytes=10-{CHUNK + 19}".encode())])
    assert [len(c) for c in ranged if c] == [CHUNK, 10]
    assert b"".join(ranged) == AUDIO[10 : CHUNK + 20]


def _open_paths() -> set[str]:
    return {os.path.realpath(f.path) for f in psutil.Process().open_files()}


class _ClientGone(OSError):
    """What uvicorn raises from send() once the client has disconnected."""


def _disconnect_after_first_chunk(path: Path) -> tuple[Callable[..., Any], list[bool]]:
    open_during_stream: list[bool] = []
    bodies = 0

    async def send(message: dict[str, Any]) -> None:
        nonlocal bodies
        if message["type"] == "http.response.body":
            bodies += 1
            open_during_stream.append(os.path.realpath(path) in _open_paths())
            if bodies == 2:
                raise _ClientGone("client went away")

    return send, open_during_stream


def test_client_disconnect_mid_stream_closes_the_audio_file(audio_file: Path) -> None:
    """[if] the client goes away after the first 2 MiB chunk and the file stays open [then] each aborted deck load leaks a handle, [else stop]."""
    app = _app("0.0.0.0")
    send, open_during_stream = _disconnect_after_first_chunk(audio_file)
    with pytest.raises(_ClientGone):
        asyncio.run(app(_scope(AUDIO_URL), _receive, send))
    # Positive control: the probe sees the file while it is being streamed.
    assert open_during_stream[0] is True
    assert os.path.realpath(audio_file) not in _open_paths()


@pytest.mark.skipif(not FD_ANCHORED_WALK_SUPPORTED, reason="no O_NOFOLLOW or fd paths: Windows serves the checked path")
@pytest.mark.requirement("USBPLAY-06")
def test_client_disconnect_mid_stream_releases_the_pinned_stick_descriptor(tmp_path: Path) -> None:
    """[if] a stick stream aborted after its first 2 MiB chunk keeps its pinned fd [then] broken, [else stop]."""
    from apps.sync.usb.stick_file_pin import pin_stick_file
    from apps.webui.server.routes.usb_tracks_files import _PinnedFileResponse

    music = tmp_path / "stick" / "Contents"
    music.mkdir(parents=True)
    track = music / "track.mp3"
    track.write_bytes(AUDIO)
    pins = []
    app = FastAPI()

    @app.get("/f")
    def serve() -> _PinnedFileResponse:
        pinned = pin_stick_file("0123ABCD-0000-0000-0000-000000000000", track.resolve())
        pins.append(pinned)
        return _PinnedFileResponse(pinned, media_type="audio/mpeg")

    assert _PinnedFileResponse.chunk_size == CHUNK
    bodies: list[int] = []
    fds: list[int] = []

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            bodies.append(len(message["body"]))
            if len(bodies) == 1:
                # Positive control: the pinned descriptor is live mid-stream.
                fd = pins[0].fd
                assert fd is not None
                os.fstat(fd)
                fds.append(fd)
            else:
                raise _ClientGone("client went away")

    with pytest.raises(_ClientGone):
        asyncio.run(app(_scope("/f"), _receive, send))
    assert bodies[0] == CHUNK
    with pytest.raises(OSError):
        os.fstat(fds[0])
