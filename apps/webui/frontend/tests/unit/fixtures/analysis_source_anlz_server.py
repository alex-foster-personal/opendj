"""Real HTTP fixture server for analysis-source-deck-refresh.test.mjs.

The regression it backs (PARITY-02's refreshDecksForAnalysisSourceChange) was
flagged BLOCKING (discussion_r3921839834) for exercising a hand-fabricated
`globalThis.fetch` response instead of the real backend: the swap the test
claims to verify - `_resolve_beatgrid_source`, the exact function the
production `/anlz` route calls (rb_assets.py) - never actually ran under the
old test, so a regression in that function would not have failed it.

This binds a REAL uvicorn server on an OS-assigned loopback port, mounting the
production ``rb_assets_router`` and its actual ``get_track_anlz`` route against
a real analysis state.db built the same way tests/test_analysis_source.py builds
one (apps.analysis.store.upsert_record, no mocked records). The seeded tracks
are deliberately unmapped, so the production route's real local-track path
supplies its honest empty vendor payload before the selected own beatgrid is
applied. `/test/requests` exposes every URL this process has actually served,
over real ASGI middleware, so the JS test can assert an unloaded deck made no
real network call without touching `globalThis.fetch` at all.

Usage: `uv run --no-sync python analysis_source_anlz_server.py`, then read the
single `READY <port>` stdout line.
"""
from __future__ import annotations

import asyncio
import socket
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request

from apps.adapters.rekordbox import config as rb_config
from apps.analysis.record import AnalysisRecord
from apps.analysis.store import upsert_record
from apps.webui.server.analysis_source import AnalysisSourceStore
from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.routes.analysis_source import router as analysis_source_router
from apps.webui.server.routes.rb_assets import router as rb_assets_router

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
    for stable_id in (SID_TRACK_A, SID_TRACK_B, SID_SLOW, SID_NO_OWN_ANALYSIS):
        _add_unmapped_track(db_path, stable_id)
    # SID_NO_OWN_ANALYSIS is intentionally never written: real "no record" case.


def _add_unmapped_track(db_path: Path, stable_id: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO tracks (stable_id, stable_id_tier, title, artists_json, "
            "file_path, created_at, updated_at, deleted_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                stable_id,
                "inferred",
                f"title-{stable_id}",
                "[]",
                str(db_path.parent / f"{stable_id}.flac"),
                "2026-09-01T00:00:00Z",
                "2026-09-01T00:00:00Z",
                None,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def create_app() -> FastAPI:
    app = FastAPI()
    rb_config.STATE_DB = _DB_PATH
    app.state.backend = InMemoryBackend()
    app.state.analysis_source = AnalysisSourceStore()
    app.state.analysis_db_path = _DB_PATH
    app.state.requests: list[str] = []
    app.include_router(analysis_source_router, prefix="/api/v1")
    app.include_router(rb_assets_router, prefix="/api/v1")

    @app.middleware("http")
    async def _record_request(request: Request, call_next):
        # Excludes its own reader: /test/requests polling for the log must
        # not append itself to the log it is about to return, or every read
        # shifts the next read's slice by one (caught live: the deck-refresh
        # test's own before/after delta was off by exactly the poll count).
        if request.url.path != "/test/requests":
            app.state.requests.append(str(request.url))
        if request.url.path.endswith(f"/{SID_SLOW}/anlz"):
            await asyncio.sleep(0.15)
        return await call_next(request)

    @app.get("/test/requests")
    def _get_requests() -> list[str]:
        return app.state.requests

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
