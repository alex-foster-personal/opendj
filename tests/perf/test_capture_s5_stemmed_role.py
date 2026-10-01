"""S5's stemmed role admits only a track with a real stem bundle (#3966).

[if] the stemmed track has no stem bundle [then] S5 writes an error row, [else stop].

``GET /api/v1/tracks/{id}/stems`` answers HTTP 200 for a track with no bundle
too (``{"status": "unavailable", "code": "STEM_BUNDLE_NOT_FOUND"}``), so the
old status-code check admitted any track and #3828's "stemmed" S5 number was a
mix-only fetch. A bundle the R2 index knows but this machine has not fetched
yet answers 200 unavailable ``STEM_BUNDLE_HYDRATING`` while the engine fetches
it, and must be polled, not refused.

Every answer here comes from the PRODUCTION stems router served over real HTTP
(uvicorn on a free port), because the capture reads the engine through
``urllib``. The hydrating state is the router's own: a real
``HubPresignedSource`` (real ``HttpTransport``) bound on ``app.state`` exactly
as ``app_wiring`` binds it, plus a real local index cache written by
``save_cached_index`` naming a real bundle's file hashes. The hub it points at
is a real TCP endpoint: either a closed port (the hub is down) or a listener
that accepts and never answers (a fetch still in flight).

Regression one-liners:
  - if a track whose /stems says STEM_BUNDLE_NOT_FOUND passes the stemmed check then broken
  - if a track with a real bundle fails the stemmed check then broken
  - if S5 writes a number when the stemmed track has no bundle then broken
  - if a manifest for another track admits this track then broken
  - if a STEM_BUNDLE_HYDRATING track is refused instead of polled until ready then broken
  - if a hydration that never finishes is waited on past the deadline then broken
  - if a hydration that fails after HYDRATING reads as "no stem bundle" then broken
"""

from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI

from apps.cloud.stem_index import save_cached_index
from apps.cloud.stem_source import HubPresignedSource
from apps.webui.server.routes.stems import router as stems_router
from scripts.perf.capture_kpi_ledger import CaptureMeta, fetch_url
from scripts.perf.capture_s5 import (
    ProbeError,
    capture,
    require_stem_bundle,
    stems_payload_is_ready,
)
from tests.webui.test_stems import _bundle

pytestmark = pytest.mark.requirement("PERF-CAPTURE-01")

STEMMED = "track-with-stems"
NO_STEMS = "track-without-stems"
FAST_POLL = {"deadline_s": 10.0, "poll_s": 0.1}


# ----------------------------------------------------------------------------- engine


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def _serve(app: FastAPI) -> Iterator[str]:
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline, "stems test engine did not start"
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _stems_app(stems_dir: Path) -> FastAPI:
    app = FastAPI()
    app.state.stems_dir = stems_dir
    app.include_router(stems_router, prefix="/api/v1")
    return app


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[str]:
    stems_dir = tmp_path / "stems"
    _bundle(stems_dir, STEMMED)
    with _serve(_stems_app(stems_dir)) as url:
        yield url


# ----------------------------------------------------------------------------- hydration


class _SilentHub:
    """A real TCP listener that accepts hub requests and never answers them.

    Holds the engine's presign fetch in flight, which is the state a slow R2
    hydration is in. ``on_accept`` runs once per accepted connection.
    """

    def __init__(self, on_accept: Callable[[], object] | None = None) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen()
        self._conns: list[socket.socket] = []
        self._on_accept = on_accept
        self.accepted = 0
        self.url = f"http://127.0.0.1:{self._listener.getsockname()[1]}"
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def _accept_loop(self) -> None:
        while True:
            try:
                conn, _addr = self._listener.accept()
            except OSError:
                return
            self._conns.append(conn)
            self.accepted += 1
            if self._on_accept is not None:
                self._on_accept()

    def close(self) -> None:
        self._listener.close()
        for conn in self._conns:
            conn.close()
        self._thread.join(timeout=5)


def _closed_port_url() -> str:
    return f"http://127.0.0.1:{_free_port()}"


def _index_real_bundle(tmp_path: Path, data_dir: Path, stable_id: str) -> None:
    """Write the local R2 index cache entry for a real bundle's file hashes."""
    published = _bundle(tmp_path / "published", stable_id)
    hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(published.iterdir())
    }
    save_cached_index(data_dir, {stable_id: hashes})


@contextmanager
def _hydrating_engine(
    tmp_path: Path,
    stable_id: str,
    hub_url: str,
) -> Iterator[str]:
    """Production router armed with hub hydration, the bundle indexed but not local."""
    data_dir = tmp_path / "data"
    _index_real_bundle(tmp_path, data_dir, stable_id)
    app = _stems_app(tmp_path / "stems")
    app.state.stem_hydration_source = HubPresignedSource(
        data_dir=data_dir,
        hub_url=hub_url,
        machine_id="s5-test-machine",
        bearer=None,
    )
    app.state.stem_hydration_data_dir = data_dir
    with _serve(app) as url:
        yield url


def _meta() -> CaptureMeta:
    return CaptureMeta(
        capture_id="perf-capture-20260929T000000Z",
        date="2026-09-29",
        machine="test-host",
        sha="0" * 40,
    )


# ----------------------------------------------------------------------------- tests


def test_a_track_with_a_real_bundle_passes(engine: str) -> None:
    """[if] the track has a real four-part bundle [then] the stemmed check passes, [else stop]."""
    require_stem_bundle(engine, STEMMED, **FAST_POLL)


def test_a_track_whose_stems_are_not_found_is_refused(engine: str) -> None:
    """[if] /stems answers 200 STEM_BUNDLE_NOT_FOUND [then] the check raises at once, [else stop]."""
    started = time.monotonic()
    with pytest.raises(ProbeError, match="has no stem bundle: STEM_BUNDLE_NOT_FOUND"):
        require_stem_bundle(engine, NO_STEMS, **FAST_POLL)
    assert time.monotonic() - started < FAST_POLL["deadline_s"], "NOT_FOUND was polled"


def test_a_real_manifest_for_another_track_is_refused(engine: str) -> None:
    """[if] the manifest names another stable_id [then] it is not this track's bundle, [else stop]."""
    status, _headers, body = fetch_url(f"{engine}/api/v1/tracks/{STEMMED}/stems", 10)
    assert status == 200
    real_manifest = json.loads(body)
    assert stems_payload_is_ready(real_manifest, STEMMED) is True
    with pytest.raises(ProbeError, match="is not a stem manifest for that track"):
        stems_payload_is_ready(real_manifest, NO_STEMS)


def test_a_hydrating_bundle_is_polled_until_its_manifest_is_ready(tmp_path: Path) -> None:
    """[if] /stems says HYDRATING then the bundle lands [then] the check passes, [else stop]."""
    stable_id = "track-hydrating-then-ready"
    # The engine opens this connection only after its manifest route answered
    # STEM_BUNDLE_HYDRATING and enqueued the fetch; the bundle lands mid-fetch.
    hub = _SilentHub(on_accept=lambda: _bundle(tmp_path / "stems", stable_id))
    try:
        with _hydrating_engine(tmp_path, stable_id, hub.url) as url:
            require_stem_bundle(url, stable_id, **FAST_POLL)
    finally:
        hub.close()
    assert hub.accepted >= 1, "the engine never enqueued a hydration: HYDRATING was not exercised"


def test_a_hydration_that_never_finishes_errors_at_the_deadline(tmp_path: Path) -> None:
    """[if] /stems stays HYDRATING past the deadline [then] the check raises, [else stop]."""
    stable_id = "track-hydrating-forever"
    hub = _SilentHub()
    try:
        with _hydrating_engine(tmp_path, stable_id, hub.url) as url:
            started = time.monotonic()
            with pytest.raises(ProbeError, match="still STEM_BUNDLE_HYDRATING after 1 s"):
                require_stem_bundle(url, stable_id, deadline_s=1.0, poll_s=0.1)
            waited = time.monotonic() - started
    finally:
        hub.close()
    assert hub.accepted >= 1, "the engine never enqueued a hydration: HYDRATING was not exercised"
    assert 1.0 <= waited < 5.0, f"waited {waited:.2f} s for a 1 s deadline"


def test_a_hydration_that_fails_is_reported_loud_not_as_no_bundle(tmp_path: Path) -> None:
    """[if] HYDRATING is followed by a failed fetch [then] the error names the HTTP failure, [else stop]."""
    stable_id = "track-hydrating-then-hub-down"
    with (
        _hydrating_engine(tmp_path, stable_id, _closed_port_url()) as url,
        pytest.raises(ProbeError) as raised,
    ):
        require_stem_bundle(url, stable_id, **FAST_POLL)
    message = str(raised.value)
    assert "returned HTTP 503 SYNC_HUB_UNREACHABLE" in message, message
    assert "has no stem bundle" not in message, message


def test_s5_writes_an_error_row_not_a_number_for_a_stemless_stemmed_track(engine: str) -> None:
    """[if] the stemmed track has no bundle [then] S5 writes an error, not a number, [else stop]."""
    rows = capture(
        engine=engine,
        meta=_meta(),
        tracks={"small": STEMMED, "large": STEMMED, "stemmed": NO_STEMS},
        data_dir=None,
    )
    assert rows, "capture wrote no row at all"
    for row in rows:
        assert row["value"] is None, row
        assert row["status"] == "error", row
    assert any(
        "stemmed:" in str(row["note"]) and "STEM_BUNDLE_NOT_FOUND" in str(row["note"])
        for row in rows
    ), rows
