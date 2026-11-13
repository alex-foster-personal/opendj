"""Shared helpers for the /api/v1/dj/try route tests (``test_dj_try_routes.py``, ``test_dj_try_start_routes.py``)
and the CLI tests that drive the same router: real WAV and candidate builders, the ``try_pair`` double behind
``dj_try_client``, and a loopback serving engine for the lanes route. Not a test module."""

from __future__ import annotations

import dataclasses
import json
import sqlite3
import wave
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from apps.autodj.propose import propose_transition
from apps.transition_score.contract import SCORER_VERSION, Candidate, Measured, TryResult, Veto
from apps.webui.server.routes import dj_try
from tests.autodj.fixtures import anlz_payload, track_row
from tests.autodj.fixtures import profile as autodj_profile
from tests.transition_score.arrangements import ARRANGEMENT, IN_ARRANGEMENT
from tests.waits import start_uvicorn_in_thread, stop_uvicorn_in_thread


def _wav(path: Path) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(48000)
        handle.writeframes(b"\x00\x00" * 2 * 480)
    return path


def _candidate(
    tmp: Path, cid: str, score: float | None, vetoes: tuple[Veto, ...] = (), unknown: dict[str, str] | None = None
) -> Candidate:
    tmp.mkdir(parents=True, exist_ok=True)
    return Candidate(
        id=cid,
        plan_json=json.dumps({"style": "blend", "bars": 16}),
        plan_sha256=f"sha-{cid}",
        style="blend",
        bars=16,
        metrics_json=json.dumps({"vocal_overlap_s": 0.0}),
        factors_json=json.dumps({"alignment": 0.9}),
        vetoes=vetoes,
        measured=Measured(k=5, n=7),
        unknown_json=json.dumps({"seam_clicks": "no render"} if unknown is None else unknown),
        score=score,
        scorer_version=SCORER_VERSION,
        render_path=str(_wav(tmp / f"{cid}.wav")),
        render_ms=10.0,
        score_ms=2.0,
    )


def _job_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


#: A test may replace the candidates the double returns (default: the three below).
OVERRIDE: dict[str, Any] = {}


@contextmanager
def dj_try_client(tmp_path: Path) -> Iterator[tuple[TestClient, list[dict[str, Any]]]]:
    """The real try router over a state.db under ``tmp_path``, with ``try_pair`` replaced by a contract-typed double;
    yields the client and the list of calls the double received (``OVERRIDE`` steers what it returns)."""
    calls: list[dict[str, Any]] = []
    OVERRIDE.clear()

    def fake_try_pair(
        out_id: str,
        in_id: str,
        *,
        n: int,
        context_bars: int,
        data_dir: Path,
        source: object,
        keep_renders_in: Path,
        autodj: object,
    ) -> TryResult:
        if "read_profile" in OVERRIDE:
            OVERRIDE["read_profile"](source)  # a test reads a profile through the route's real source
        if "raise" in OVERRIDE:
            if OVERRIDE.get("write_first"):
                _wav(_job_dir(keep_renders_in) / "partial.wav")  # renders already kept when the failure hits
            raise OVERRIDE["raise"]  # type: ignore[misc]
        calls.append(
            {
                "out": out_id,
                "in": in_id,
                "n": n,
                "context_bars": context_bars,
                "data_dir": data_dir,
                "source": source,
                "keep_renders_in": keep_renders_in,
            }
        )
        overridden = tuple(
            # Like the real try_pair, a kept render lives in this job's folder: a pre-built one is copied there.
            dataclasses.replace(c, render_path=str(_wav(_job_dir(keep_renders_in) / f"{c.id}.wav")))
            if c.render_path is not None
            else c
            for c in OVERRIDE.get("candidates", ())
        )
        if OVERRIDE.get("stray"):
            overridden = OVERRIDE["candidates"]  # a defective runner: renders kept outside the job folder
        return TryResult(
            out_id=out_id,
            in_id=in_id,
            candidates=overridden
            or (
                _candidate(keep_renders_in, "A", 0.8),
                _candidate(keep_renders_in, "B", 0.0, (Veto("kick_flam_ms", 18.0, 10.0),)),
                _candidate(keep_renders_in, "C", None),
            ),
        )

    (tmp_path / "state").mkdir()
    app = FastAPI()
    app.state.state_db_path = str(tmp_path / "state" / "state.db")
    app.include_router(dj_try.router, prefix="/api/v1")
    app.dependency_overrides[dj_try.get_try_runner] = lambda: fake_try_pair
    # Explicit, not the tests/conftest.py default, since the origin assertion below depends on it.
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client, calls


def _start(test_client: TestClient) -> dict[str, Any]:
    response = test_client.post("/api/v1/dj/try", json={"out": "trk-out", "in": "trk-in", "n": 3, "context_bars": 4})
    assert response.status_code == 200, response.text
    return response.json()


def _stored_jobs(tmp_path: Path) -> int:
    with sqlite3.connect(tmp_path / "state" / "state.db") as state:
        tables = {row[0] for row in state.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "transition_try_jobs" not in tables:
            return 0
        return int(state.execute("SELECT COUNT(*) FROM transition_try_jobs").fetchone()[0])


#: The route profiles the pair over HTTP from its OWN socket, so these tests serve the try router and the track and
#: anlz payloads it reads from one uvicorn app; ``LANE_ENGINE["profile"]`` picks how those payload routes answer.
LANE_OUT, LANE_IN, LANE_BPM = "fixture-out", "fixture-in", 124.0
LANE_TRACKS = {LANE_OUT: ("8A", ARRANGEMENT), LANE_IN: ("9A", IN_ARRANGEMENT)}
LANE_ENGINE: dict[str, str] = {}


def _keep_one_crossfade(out_id: str, in_id: str, *, keep_renders_in: Path, **_kwargs: Any) -> TryResult:
    """Stands in for try_pair: keeps one real crossfade plan with its mix and both deck renders on disk."""
    out_key, out_arrangement = LANE_TRACKS[out_id]
    in_key, in_arrangement = LANE_TRACKS[in_id]
    proposals = propose_transition(
        autodj_profile(out_id, bpm=LANE_BPM, key=out_key, arrangement=out_arrangement),
        autodj_profile(in_id, bpm=LANE_BPM, key=in_key, arrangement=in_arrangement),
        out_deck=1,
        in_deck=2,
        top=12,
    )["proposals"]
    plan = next(p["plan"] for p in proposals if p["plan"]["style"] == "crossfade")
    folder = keep_renders_in / "A"
    for deck in (1, 2):
        _wav(_job_dir(folder / "decks") / f"deck{deck}.wav")
    kept = _candidate(folder, "A", 0.8)
    return TryResult(
        out_id=out_id,
        in_id=in_id,
        candidates=(dataclasses.replace(kept, plan_json=json.dumps(plan), style="crossfade"),),
    )


@contextmanager
def _lanes_engine(tmp_path: Path) -> Iterator[str]:
    """The real try router plus the profile payloads it reads, on loopback, with one job stored; yields its lanes URL."""
    app = FastAPI()
    (tmp_path / "state").mkdir()
    app.state.state_db_path = str(tmp_path / "state" / "state.db")
    app.include_router(dj_try.router, prefix="/api/v1")
    app.dependency_overrides[dj_try.get_try_runner] = lambda: _keep_one_crossfade
    LANE_ENGINE["profile"] = "valid"

    @app.get("/api/v1/tracks/{stable_id}")
    def track(stable_id: str) -> Any:
        if LANE_ENGINE["profile"] == "engine_500":
            return JSONResponse(status_code=500, content={"error": "internal", "message": "state.db is locked"})
        key, arrangement = LANE_TRACKS[stable_id]
        return track_row(stable_id, bpm=LANE_BPM, key=key, bars=sum(row[1] for row in arrangement))

    @app.get("/api/v1/tracks/{stable_id}/anlz")
    def anlz(stable_id: str) -> Any:
        if LANE_ENGINE["profile"] == "no_analysis" and stable_id == LANE_IN:
            return JSONResponse(status_code=404, content={"error": "not_analysed", "message": "no analysis"})
        return anlz_payload(stable_id, bpm=LANE_BPM, arrangement=LANE_TRACKS[stable_id][1])

    engine, thread = start_uvicorn_in_thread(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"), what="try-er lanes engine"
    )
    url = f"http://127.0.0.1:{engine.servers[0].sockets[0].getsockname()[1]}"
    try:
        job = httpx.post(f"{url}/api/v1/dj/try", json={"out": LANE_OUT, "in": LANE_IN, "n": 1}, timeout=30.0)
        assert job.status_code == 200, job.text
        yield f"{url}/api/v1/dj/try/{job.json()['job_id']}/candidates/A/lanes?columns=40"
    finally:
        stop_uvicorn_in_thread(engine, thread, what="try-er lanes engine")
