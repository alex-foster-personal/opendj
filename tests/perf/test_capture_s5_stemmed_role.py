"""S5's stemmed role admits only a track with a real stem bundle (#3966).

[if] the stemmed track has no stem bundle [then] S5 writes an error row, [else stop].

``GET /api/v1/tracks/{id}/stems`` answers HTTP 200 for a track with no bundle
too (``{"status": "unavailable", "code": "STEM_BUNDLE_NOT_FOUND"}``), so the
old status-code check admitted any track and #3828's "stemmed" S5 number was a
mix-only fetch. These tests serve the PRODUCTION stems router over real HTTP
(uvicorn on a free port), one track with a real four-part WAV bundle and one
without, because the capture reads the engine through ``urllib``.

Regression one-liners:
  - if a track whose /stems says unavailable passes the stemmed check then broken
  - if a track with a real bundle fails the stemmed check then broken
  - if S5 writes a number when the stemmed track has no bundle then broken
  - if a 200 body that is not this track's manifest admits the track then broken
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI

from apps.webui.server.routes.stems import router as stems_router
from scripts.perf.capture_kpi_ledger import CaptureMeta
from scripts.perf.capture_s5 import ProbeError, capture, require_stem_bundle
from tests.webui.test_stems import _bundle

pytestmark = pytest.mark.requirement("PERF-CAPTURE-01")

STEMMED = "track-with-stems"
NO_STEMS = "track-without-stems"
MISFILED = "track-served-another-manifest"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[str]:
    stems_dir = tmp_path / "stems"
    _bundle(stems_dir, STEMMED)
    app = FastAPI()
    app.state.stems_dir = stems_dir

    # Registered before the router so it wins for this one id: an engine that
    # answers 200 with SOME track's manifest, the shape check's only subject.
    @app.get(f"/api/v1/tracks/{MISFILED}/stems")
    def _another_tracks_manifest() -> dict:
        return {"stable_id": STEMMED, "parts": {"vocals": {"media_type": "audio/wav"}}}

    app.include_router(stems_router, prefix="/api/v1")
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


def _meta() -> CaptureMeta:
    return CaptureMeta(
        capture_id="perf-capture-20260929T000000Z",
        date="2026-09-29",
        machine="test-host",
        sha="0" * 40,
    )


def test_a_track_with_a_real_bundle_passes(engine: str) -> None:
    """[if] the track has a real four-part bundle [then] the stemmed check passes, [else stop]."""
    require_stem_bundle(engine, STEMMED)


def test_a_track_whose_stems_are_unavailable_is_refused(engine: str) -> None:
    """[if] /stems answers 200 unavailable [then] the check raises naming the code, [else stop]."""
    with pytest.raises(ProbeError, match="STEM_BUNDLE_NOT_FOUND"):
        require_stem_bundle(engine, NO_STEMS)


def test_a_manifest_for_another_track_is_refused(engine: str) -> None:
    """[if] /stems answers 200 with another track's manifest [then] it raises, [else stop]."""
    with pytest.raises(ProbeError, match="is not a stem manifest for that track"):
        require_stem_bundle(engine, MISFILED)


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
