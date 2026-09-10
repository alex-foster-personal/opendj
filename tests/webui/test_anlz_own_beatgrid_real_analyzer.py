"""Codex P1 BLOCKING (PR #1587, `test_anlz_own_beatgrid_route.py:62`): the
promotion precondition must be demonstrated against the real production
analyzer over real hydrated media, not by replaying a captured payload
through `_write_own_record` alone.

`_write_own_record` (see `test_anlz_own_beatgrid.py`) is not a mock: it feeds
a real captured Beat This! artifact (`ops/beatbench/round-1/
raw-beatgrid_lane_t050.json`) through the real `record_from_payload` and
`upsert_record` production functions, which is exactly the "captured real
protocol payload, consumed through production paths" AGENTS.md permits
(AGENTS.md:L125-L133). What it never exercises is the SUBPROCESS itself: the
real `beat_this_runner.py` invocation, driven from real audio, through
`OwnBeatgridBackfillBackend.analyze`. This test is that other half, driven
through the real HTTP route the way `test_anlz_own_beatgrid_route.py`'s other
tests are.

WHY THIS IS OPT-IN, AND WHY THE GATE NAMES A CHECKPOINT RATHER THAN A NETWORK
FETCH. `tests/analysis_beatgrid/test_mutation_end_to_end.py` invokes
`beat_this_runner.py` directly with no `--checkpoint`, so Beat This! resolves
its bare `final0` name through a Torch Hub download on first run. The
PRODUCTION path this test exercises never does that: `OwnBeatgridBackfillBackend
.analyze` resolves weights through `apps.analysis_beatgrid.weights
.resolve_checkpoint`, which by design has NO network fallback (see that
module's docstring: production must stay network-free after install). So this
test cannot self-provision a checkpoint the way the mutation suite can. It
skips with a named, actionable reason when none is present, exactly the
"fail explicitly or report UNAVAILABLE" AGENTS.md asks for, rather than either
downloading around the production guard or fabricating a result.

To run this for real: provision a checkpoint (`python -m
apps.analysis_beatgrid.weights install --from <path-to-beat_this-final0.ckpt>`,
or point `MDT_BEATGRID_WEIGHTS` at one) and set MDT_BEATGRID_MODEL_TESTS=1.

-Claude Sonnet 5
"""
from __future__ import annotations

import math
import os
import sqlite3
import struct
import wave
from pathlib import Path

import pytest

from apps.adapters.rekordbox import config as rb_config
from apps.analysis import selection
from apps.analysis.backends.base import BackendNotAvailable
from apps.analysis.backends.own_beatgrid import OwnBeatgridBackfillBackend
from apps.analysis.store import upsert_record

pytestmark = pytest.mark.skipif(
    os.environ.get("MDT_BEATGRID_MODEL_TESTS") != "1",
    reason=(
        "drives the real Beat This! analyzer over real audio through the real "
        "HTTP route; needs uv, ffmpeg and a provisioned checkpoint. "
        "set MDT_BEATGRID_MODEL_TESTS=1 to run"
    ),
)

SAMPLE_RATE = 44100
CLICK_BPM = 128.0
DURATION_S = 8.0


def _write_real_click_track(path: Path) -> None:
    """A short, genuinely decodable 4/4 click track at a known tempo.

    Real PCM samples with actual signal in them, not the fabricated
    header-only zero-frame WAV Codex's finding calls out: a real decode is
    exactly the step that stub skipped.
    """
    period = 60.0 / CLICK_BPM
    n_samples = int(DURATION_S * SAMPLE_RATE)
    samples = [0.0] * n_samples
    beat = 0
    t0 = 0.0
    while t0 < DURATION_S:
        start = int(t0 * SAMPLE_RATE)
        burst = int(0.03 * SAMPLE_RATE)
        is_downbeat = beat % 4 == 0
        freq = 1800.0 if is_downbeat else 1200.0
        amp = 0.9 if is_downbeat else 0.45
        for i in range(burst):
            if start + i >= n_samples:
                break
            envelope = math.exp(-i / (0.01 * SAMPLE_RATE))
            samples[start + i] += amp * envelope * math.sin(
                2 * math.pi * freq * i / SAMPLE_RATE
            )
        beat += 1
        t0 += period

    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(SAMPLE_RATE)
        fh.writeframes(
            b"".join(
                struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32000)) for s in samples
            )
        )


def test_the_promotion_precondition_holds_against_the_real_analyzer_and_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The promotion gate, with a real subprocess run and a real HTTP call.

    `record_from_payload`/`upsert_record` are already exercised against real
    captured output in `test_anlz_own_beatgrid.py` and
    `test_backfill_write.py`. What was still missing, and what Codex's finding
    names precisely, is the subprocess boundary: this calls
    `OwnBeatgridBackfillBackend.analyze` itself, which shells out to the real
    `beat_this_runner.py` over the real audio file below, before writing
    through the same production store and serving through the same route the
    other tests in this module drive.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apps.shared.state import db as state_db
    from apps.shared.state.events import FakeEventBus
    from apps.shared.state.writer import StateWriter
    from apps.webui.server.routes.rb_assets import router as assets_router
    from apps.webui.server.sqlite_backend import make_backend

    sid = "a" * 40
    audio = tmp_path / "click.wav"
    _write_real_click_track(audio)

    state_path = tmp_path / "state.db"
    conn = state_db.open_rw(state_path)
    writer = StateWriter(conn, bus=FakeEventBus(), actor="test-seed")
    try:
        writer.upsert_track(
            stable_id=sid, stable_id_tier="inferred", title=None, artists=[],
            album=None, isrc=None, duration_ms=int(DURATION_S * 1000),
            file_path=str(audio),
        )
    finally:
        writer.close()
        conn.close()

    try:
        record = OwnBeatgridBackfillBackend.analyze(audio, sid)
    except BackendNotAvailable as exc:
        pytest.skip(f"own beatgrid backend not available on this host: {exc}")
    upsert_record(record, db_path=state_path)

    sel_conn = sqlite3.connect(state_path)
    try:
        selection.set_default(sel_conn, "beatgrid", "own")
        sel_conn.commit()
    finally:
        sel_conn.close()

    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", tmp_path / "absent.db")

    app = FastAPI()
    app.state.backend = make_backend()
    app.state.analysis_db_path = state_path
    app.include_router(assets_router, prefix="/api/v1")

    with TestClient(app) as client:
        response = client.get(f"/api/v1/tracks/{sid}/anlz")

    assert response.status_code == 200
    body = response.json()
    assert body["beatgrid"]["source"] == "own"
    assert body["beatgrid"]["status"] == "ok", (
        "promotion gate not satisfiable: the real analyzer/route chain served "
        f"{body['beatgrid']!r} instead of a real grid"
    )
    assert body["beatgrid"]["beats"], "the real analyzer produced no beats"
    assert body["beatgrid"]["beat_count"] == len(body["beatgrid"]["beats"])
    assert 90.0 < body["beatgrid"]["bpm"] < 170.0, (
        f"real analyzer read {body['beatgrid']['bpm']} BPM off a {CLICK_BPM} "
        "BPM click track, which is outside its octave-error tolerance"
    )
    assert response.headers["cache-control"] == "private, no-cache"
    assert response.headers["etag"]
