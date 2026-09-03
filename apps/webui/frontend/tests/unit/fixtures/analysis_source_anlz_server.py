"""Real HTTP fixture server for analysis-source-deck-refresh.test.mjs.

The regression it backs (PARITY-02's refreshDecksForAnalysisSourceChange) was
flagged BLOCKING (discussion_r3921839834) for exercising a hand-fabricated
`globalThis.fetch` response instead of the real backend: the swap the test
claims to verify - `_resolve_beatgrid_source`, the exact function the
production `/anlz` route calls (rb_assets.py) - never actually ran under the
old test, so a regression in that function would not have failed it.

This binds a REAL uvicorn server on an OS-assigned loopback port, serving
`/api/v1/tracks/{stable_id}/anlz` through the production
`_resolve_beatgrid_source` against a real analysis state.db built the same
way tests/test_analysis_source.py builds one (apps.analysis.store.upsert_record,
no mocked records). The base payload this endpoint starts from (waveform/cues/
phrases) is the one part no real rekordbox or audio fixture can supply without
the full e2e harness (tests/e2e/support/deckload_fixture.py) - it stays a
fixed, honestly-empty template exactly like the payload shape
tests/test_rb_assets.py exercises against real vendor data; only the beatgrid
swap under test runs through the real production function. `/test/requests`
exposes every URL this process has actually served, over real ASGI middleware,
so the JS test can assert an unloaded deck made no real network call without
touching `globalThis.fetch` at all.

Usage: `uv run --no-sync python analysis_source_anlz_server.py`, then read the
single `READY <port>` stdout line.
"""
from __future__ import annotations

import socket
import sys
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPOSITORY_ROOT))

from apps.analysis.record import AnalysisRecord  # noqa: E402
from apps.analysis.store import upsert_record  # noqa: E402
from apps.webui.server.analysis_source import AnalysisSourceStore  # noqa: E402
from apps.webui.server.routes.analysis_source import router as analysis_source_router  # noqa: E402
from apps.webui.server.routes.rb_assets import _resolve_beatgrid_source  # noqa: E402

BPM = 120.0
BAR_S = 240.0 / BPM

#: stable_ids the JS test loads onto decks. Deliberately different downbeat
#: counts so "own" swaps in a REAL, DISTINGUISHABLE beat_count per track
#: (mirrors the old fabricated 10-vs-20 assertion, now measured for real).
SID_TRACK_A = "real-track-a-own-grid"
SID_TRACK_B = "real-track-b-own-grid"
SID_NO_OWN_ANALYSIS = "real-track-c-no-own-analysis"
#: Artificially slow handler, for the mid-request deck-swap race test - a real
#: coroutine suspension, not a fabricated fetch delay.
SID_SLOW = "real-track-slow-own-grid"

_DB_PATH = Path(__file__).resolve().parent / ".analysis-source-anlz-server.tmp.db"


class _RequestShim:
    """Duck-types the one attribute path `_resolve_beatgrid_source` and
    `analysis._analysis_db_path` read (`request.app.state.X`) - same
    convention as test_analysis_source.py's `_FakeRequest`, pointed at the
    REAL app instance instead of a fake one, since this server's app.state
    genuinely carries a live AnalysisSourceStore and analysis_db_path."""

    def __init__(self, app: FastAPI) -> None:
        self.app = app


def _record(sid: str, bar_count: int) -> AnalysisRecord:
    return AnalysisRecord(
        stable_id=sid,
        backend="librosa+madmom",
        backend_version="test-1.0",
        analyzed_at=datetime(2026, 9, 1, tzinfo=UTC),
        duration_s=bar_count * BAR_S + 5.0,
        sample_rate=44100,
        bpm=BPM, bpm_confidence=0.9,
        key_camelot="8A", key_openkey="8m", key_confidence=0.9,
        energy=6,
        onsets_s=[],
        downbeats_s=[i * BAR_S for i in range(bar_count)],
        features_blob={"rms": [], "rms_hop": 512},
    )


def _seed_db(db_path: Path) -> None:
    if db_path.exists():
        db_path.unlink()
    upsert_record(_record(SID_TRACK_A, bar_count=10), db_path=db_path)
    upsert_record(_record(SID_TRACK_B, bar_count=20), db_path=db_path)
    upsert_record(_record(SID_SLOW, bar_count=3), db_path=db_path)
    # SID_NO_OWN_ANALYSIS is intentionally never written: real "no record" case.


def _base_payload(stable_id: str) -> dict:
    """Honest placeholder for the parts no fixture here can supply for real
    (waveform samples, cues, phrases) - see module docstring. Only the
    beatgrid/beatgrid_source/beatgrid_own_unavailable_reason fields this test
    cares about are then overwritten by the REAL production function below."""
    return {
        "stable_id": stable_id,
        "points": 38400,
        "waveform": {
            "kind": "mono",
            "preview": {"length": 0, "low": [], "mid": [], "high": []},
            "detail": {"length": 0, "low": [], "mid": [], "high": []},
        },
        "beatgrid": {"beat_count": 0, "beats": []},
        "cues": [],
        "phrases": [],
        "vocals": {"status": "not_analyzed"},
    }


def create_app() -> FastAPI:
    app = FastAPI()
    app.state.analysis_source = AnalysisSourceStore()
    app.state.analysis_db_path = _DB_PATH
    app.state.requests: list[str] = []
    app.include_router(analysis_source_router, prefix="/api/v1")

    @app.middleware("http")
    async def _record_request(request: Request, call_next):
        # Excludes its own reader: /test/requests polling for the log must
        # not append itself to the log it is about to return, or every read
        # shifts the next read's slice by one (caught live: the deck-refresh
        # test's own before/after delta was off by exactly the poll count).
        if request.url.path != "/test/requests":
            app.state.requests.append(str(request.url))
        return await call_next(request)

    @app.get("/test/requests")
    def _get_requests() -> list[str]:
        return app.state.requests

    @app.get("/api/v1/tracks/{stable_id}/anlz")
    async def get_anlz(stable_id: str, points: int = 38400) -> JSONResponse:
        if stable_id == SID_SLOW:
            import asyncio

            await asyncio.sleep(0.15)
        payload = _base_payload(stable_id)
        payload["points"] = points
        # THE call under test: the exact function the production /anlz route
        # (rb_assets.py get_track_anlz) uses to apply the rbx-vs-own swap.
        _resolve_beatgrid_source(_RequestShim(app), stable_id, payload)
        return JSONResponse(payload)

    return app


def main() -> int:
    _seed_db(_DB_PATH)
    app = create_app()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    print(f"READY {port}", flush=True)
    config = uvicorn.Config(app, fd=sock.fileno(), log_level="warning")
    server = uvicorn.Server(config)
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
