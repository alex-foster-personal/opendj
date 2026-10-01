"""Real HTTP fixture server for analysis-source-deck-refresh.test.mjs.

The regression it backs (PARITY-02's refreshAnalysisSourceDecks) was
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
import hashlib
import shutil
import signal
import socket
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request

from apps.adapters.rekordbox import config as rb_config
from apps.analysis.lanes import LaneResult
from apps.analysis.record import AnalysisRecord
from apps.analysis.store import upsert_record
from apps.webui.server import analysis_serving_bootstrap  # noqa: F401 - PARITY-02 lanes
from apps.webui.server.backend import BackendError, ConflictError, InMemoryBackend, NotFoundError
from apps.webui.server.errors import handle_backend_error, handle_conflict, handle_not_found
from apps.webui.server.routes.analysis_source import router as analysis_source_router
from apps.webui.server.routes.rb_assets import router as rb_assets_router
from apps.webui.server.routes.tracks import router as tracks_router
from apps.webui.server.sqlite_backend import SqliteBackend

BPM = 120.0
BAR_S = 240.0 / BPM
#: The rekordbox tag BPM each seeded track carries in ``track_fields``, one
#: distinct value per track. ``bpm`` is a LANE-OWNED projection field on the
#: beatgrid lane (apps/analysis/canonical.py PROJECTION_FIELDS), which is what
#: makes ``GET /tracks/{id}`` part of an analysis-source switch rather than a
#: one-shot read at load(): the JS test asserts a deck re-reads this row.
ROW_BPM: dict[str, float] = {}

#: stable_ids the JS test loads onto decks. Deliberately different downbeat
#: counts so "own" swaps in a REAL, DISTINGUISHABLE beat_count per track
#: (mirrors the old fabricated 10-vs-20 assertion, now measured for real).
SID_TRACK_A = "real-track-a-own-grid"
SID_TRACK_B = "real-track-b-own-grid"
SID_NO_OWN_ANALYSIS = "real-track-c-no-own-analysis"
#: Artificially slow handler, for the mid-request deck-swap race test - a real
#: coroutine suspension, not a fabricated fetch delay.
SID_SLOW = "real-track-slow-own-grid"
#: Seeded in NEITHER table, so the production route raises its real 404, and
#: delayed by the same real suspension as SID_SLOW. The delay is what makes the
#: all-or-nothing test bite: with a fast 404 the rejection can beat a
#: concurrently-succeeding track's own continuation, so a partial-write
#: implementation passes by luck rather than by being correct.
SID_SLOW_ABSENT = "slow-absent-track"
#: RBX-lane bpm sourced from MIK, not rekordbox, and no own analysis record.
#: Proves the OWN grid/tempo pairing guard (discussion_r3972264411) does not
#: mistake "provenance is not literally rekordbox" for "provenance is own" -
#: a non-rekordbox track_fields writer (MIK, djay, manual, ...) is still a
#: legitimate RBX-lane source, not an own one (discussion_r3974235445 P1
#: BLOCKING).
SID_MIK_BPM = "real-track-mik-bpm-no-own-analysis"

#: Per-run scratch dir OUTSIDE the tracked tree. Opening the state db
#: regenerates a sibling AGENTS.md (apps/shared/state/db.py), so a db kept
#: beside this file rewrote a tracked file and leaked db/-wal/-shm on every run.
#: `SCRATCH <dir>` is printed before READY so the hygiene test can prove removal.
_SCRATCH_DIR = Path(tempfile.mkdtemp(prefix="analysis-source-anlz-server-"))
_DB_PATH = _SCRATCH_DIR / "state.db"

#: Beats per bar, so ``bar_count`` still reads as bars at the call sites.
BEATS_PER_BAR = 4
BEAT_S = 60.0 / BPM


def _beatgrid_payload(bar_count: int) -> dict:
    """A real ``own_beatgrid`` lane payload: the exact shape the deck consumes.

    ``beats`` is already ``{t, n, bpm}`` here -- the wire shape ``/anlz``
    serves and `beat-sync-math.ts` `validateBeatGrid` accepts -- and
    `apps.analysis.lane_payloads._validate_beats` enforces that at the write
    boundary, so an invalid grid fails this server's startup rather than
    reaching a JS test as a fabricated payload.
    """
    beats = [
        {"t": round(i * BEAT_S, 3), "n": (i % BEATS_PER_BAR) + 1, "bpm": BPM}
        for i in range(bar_count * BEATS_PER_BAR)
    ]
    return {
        "beats": beats,
        "bpm": BPM,
        "bpm_confidence": 0.93,
        "octave_reason": "measured",
        "first_downbeat_s": beats[0]["t"],
        "tempo_changes": [],
        "static_grid_untrusted": False,
    }


def _record(sid: str, bar_count: int) -> AnalysisRecord:
    """A NATIVE own beatgrid record, the one analysis_canonical points at.

    Was ``backend="librosa+madmom"`` until Wed 9 Sep 2026
    (discussion_r3969942709). That is a LEGACY row, and the production
    ``/anlz`` own branch now resolves the beatgrid lane through the
    ``analysis_canonical`` pointer -- the same pointer ``analysis_projection``
    derives ``bpm`` from -- which is only ever an ``own_*`` backend. Seeding a
    legacy row here made the fixture unable to exercise the native producer at
    all. Acceptance impact: strictly stronger, the served grid is now the
    canonical record's own measured beats.
    """
    return AnalysisRecord(
        stable_id=sid,
        backend="own_beatgrid.inapp",
        backend_version="1.0.0",
        analyzed_at=datetime(2026, 9, 1, tzinfo=UTC),
        duration_s=bar_count * BAR_S + 5.0,
        sample_rate=44100,
        bpm=BPM, bpm_confidence=0.9,
        key_camelot="8A", key_openkey="8m", key_confidence=0.9,
        energy=6,
        onsets_s=[],
        downbeats_s=[i * BAR_S for i in range(bar_count)],
        features_blob={"rms": [], "rms_hop": 512},
        producer="inapp",
        producer_version="1.0.0",
        # A REAL sha256 of the fixture's own identity, not a label: the record
        # contract rejects an unverifiable hash outright.
        decode_fingerprint=f"sha256:{hashlib.sha256(sid.encode()).hexdigest()}",
        lanes={"beatgrid": LaneResult(
            status="ok", confidence=0.93, payload=_beatgrid_payload(bar_count),
        )},
    )


def _seed_db(db_path: Path) -> None:
    if db_path.exists():
        db_path.unlink()
    upsert_record(_record(SID_TRACK_A, bar_count=10), db_path=db_path)
    upsert_record(_record(SID_TRACK_B, bar_count=20), db_path=db_path)
    upsert_record(_record(SID_SLOW, bar_count=3), db_path=db_path)
    for offset, stable_id in enumerate((SID_TRACK_A, SID_TRACK_B, SID_SLOW, SID_NO_OWN_ANALYSIS)):
        _add_unmapped_track(db_path, stable_id, row_bpm=90.0 + offset)
    # SID_NO_OWN_ANALYSIS is intentionally never written: real "no record" case.
    _add_unmapped_track(db_path, SID_MIK_BPM, row_bpm=128.0, bpm_source="mik")


def _add_unmapped_track(
    db_path: Path, stable_id: str, row_bpm: float, bpm_source: str = "rekordbox"
) -> None:
    """Seed the track row AND its tag BPM in ``track_fields``.

    The BPM is what ``GET /tracks/{stable_id}`` answers with, through the real
    ``SqliteBackend`` read model, so a JS test can prove a deck re-read the row
    on a source switch rather than kept the value it captured at load().

    ``bpm_source`` defaults to ``rekordbox`` like every other seeded track;
    ``SID_MIK_BPM`` is the one caller that overrides it, to prove the RBX
    lane accepts a non-rekordbox tag writer.
    """
    ROW_BPM[stable_id] = row_bpm
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
        for field_name, value_json, source in (
            ("bpm", str(row_bpm), bpm_source),
            ("key", '"8A"', "rekordbox"),
        ):
            conn.execute(
                "INSERT INTO track_fields (stable_id, field_name, value_json, source, "
                "confidence, modified_at) VALUES (?,?,?,?,?,?)",
                (stable_id, field_name, value_json, source, None, "2026-09-01T00:00:00Z"),
            )
        conn.commit()
    finally:
        conn.close()


def create_app() -> FastAPI:
    app = FastAPI()
    rb_config.STATE_DB = _DB_PATH
    # The REAL sqlite read model, not InMemoryBackend: `bpm` reaches
    # `GET /tracks/{id}` through `_row_to_track` -> `lane_owned_fields`, which
    # is the production path that makes the field source-dependent at all.
    app.state.backend = SqliteBackend(_DB_PATH, fallback=InMemoryBackend())
    app.state.state_db_path = str(_DB_PATH)
    app.state.analysis_db_path = _DB_PATH
    app.state.requests = []
    app.state.delay_next_analysis_source_get = False
    # Plain assignments: mypy rejects an annotation on a non-self attribute
    # ("Type cannot be declared in assignment to non-self attribute"), and the
    # quality ratchet counts each one. The shapes are str | None and
    # asyncio.Event | None; see _hold_if_armed and _register_hold_controls.
    app.state.hold_next_anlz_stable_id = None
    app.state.hold_next_anlz_release = None
    app.state.hold_next_track_stable_id = None
    app.state.hold_next_track_release = None
    app.include_router(analysis_source_router, prefix="/api/v1")
    app.include_router(rb_assets_router, prefix="/api/v1")
    app.include_router(tracks_router, prefix="/api/v1")
    # The same handlers app.py registers, so a stable_id the library does not
    # have answers 404 here exactly as it does in production rather than
    # leaking a 500 from an unhandled NotFoundError.
    app.add_exception_handler(NotFoundError, handle_not_found)
    app.add_exception_handler(ConflictError, handle_conflict)
    app.add_exception_handler(BackendError, handle_backend_error)

    @app.get("/test/row-bpm")
    def _row_bpm() -> dict[str, float]:
        """What the JS test expects each deck to end up holding."""
        return ROW_BPM

    @app.middleware("http")
    async def _record_request(request: Request, call_next):
        # Excludes its own reader: /test/requests polling for the log must
        # not append itself to the log it is about to return, or every read
        # shifts the next read's slice by one (caught live: the deck-refresh
        # test's own before/after delta was off by exactly the poll count).
        if request.url.path != "/test/requests":
            app.state.requests.append(str(request.url))
        # Both holds sit BEFORE the handler runs, unlike the post-handler
        # sleeps below: the handler is what reads the daemon's toggle, so a
        # hold is the only way to make one of a refresh's two parallel fetches
        # observe a switch that landed after its sibling was served - the
        # ordering a loaded runner produces by itself (nucbox-wsl-23, run
        # 35731185371: "the two parallel fetches landed on different sides
        # of a source switch" thrown by a superseded switch).
        await _hold_if_armed(app, "anlz", request, lambda sid: f"/{sid}/anlz")
        await _hold_if_armed(app, "track", request, lambda sid: f"/tracks/{sid}")
        response = await call_next(request)
        if (
            request.method == "GET"
            and request.url.path == "/api/v1/analysis/source"
            and app.state.delay_next_analysis_source_get
        ):
            app.state.delay_next_analysis_source_get = False
            await asyncio.sleep(0.15)
        elif request.url.path.endswith((f"/{SID_SLOW}/anlz", f"/{SID_SLOW_ABSENT}/anlz")) or (
            f"/tracks/{SID_SLOW_ABSENT}" in request.url.path
        ):
            # SID_SLOW_ABSENT 404s on BOTH /anlz AND /tracks/{id} (it is in
            # neither table). Promise.all in refreshAnalysisSourceDecks rejects
            # on the first of those, so delaying only /anlz left GET /tracks
            # failing in a few ms and collapsed the window the rollback-generation
            # test needs to seed a same-generation cache entry.
            await asyncio.sleep(0.15)
        return response

    @app.get("/test/requests")
    def _get_requests() -> list[str]:
        return app.state.requests

    @app.post("/test/shutdown")
    def _shutdown() -> dict[str, bool]:
        # Cooperative stop: `ChildProcess.kill()` on Windows terminates the process
        # outright, so neither the SIGTERM handler nor main()'s cleanup would run.
        app.state.server.should_exit = True
        return {"stopping": True}

    @app.post("/test/delay-next-analysis-source-get")
    def _delay_next_analysis_source_get() -> dict[str, bool]:
        app.state.delay_next_analysis_source_get = True
        return {"armed": True}

    _register_hold_controls(app, "anlz")
    _register_hold_controls(app, "track")
    return app


def _register_hold_controls(app: FastAPI, kind: str) -> None:
    """`POST /test/hold-next-{kind}` with `{"stable_id": ...}` arms a hold on the
    next matching GET (`anlz`: `/{sid}/anlz`; `track`: `/tracks/{sid}`), which the
    request middleware then parks BEFORE its handler runs until
    `POST /test/release-held-{kind}` (or 30 s, then the held request errors)."""

    @app.post(f"/test/hold-next-{kind}")
    async def _hold_next(request: Request) -> dict[str, str | bool]:
        body = await request.json()
        stable_id = body.get("stable_id")
        if not stable_id:
            raise ValueError("stable_id is required")
        setattr(app.state, f"hold_next_{kind}_stable_id", stable_id)
        setattr(app.state, f"hold_next_{kind}_release", asyncio.Event())
        return {"armed": True, "stable_id": stable_id}

    @app.post(f"/test/release-held-{kind}")
    def _release_held() -> dict[str, bool]:
        release = getattr(app.state, f"hold_next_{kind}_release")
        if release is not None:
            release.set()
        return {"released": True}


async def _hold_if_armed(app: FastAPI, kind: str, request: Request, suffix_of) -> None:
    """Park `request` until the armed hold of `kind` is released, if it is the GET
    that hold names; a hold is consumed by the first request it matches."""
    held = getattr(app.state, f"hold_next_{kind}_stable_id")
    if not held or request.method != "GET" or not request.url.path.endswith(suffix_of(held)):
        return
    setattr(app.state, f"hold_next_{kind}_stable_id", None)
    release = getattr(app.state, f"hold_next_{kind}_release")
    if release is None:
        return
    try:
        await asyncio.wait_for(release.wait(), timeout=30.0)
    except TimeoutError:
        raise RuntimeError(
            f"held {kind} GET for {held} timed out waiting for /test/release-held-{kind}"
        ) from None


def main() -> int:
    # Preferred stop is POST /test/shutdown (portable). A SIGTERM fallback still
    # cleans up on POSIX: uvicorn shuts down, restores the handler installed before
    # it, then re-raises the signal, and the default handler would skip the cleanup.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        _serve()
    finally:
        shutil.rmtree(_SCRATCH_DIR)
    return 0


def _serve() -> None:
    _seed_db(_DB_PATH)
    app = create_app()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    # listen() BEFORE the READY line: uvicorn only puts the fd into LISTEN
    # state inside server.run()'s own startup, so a reader that connects the
    # instant it sees READY can race that and get ECONNREFUSED. Calling
    # listen() here queues any early connection in the backlog instead.
    sock.listen()
    port = sock.getsockname()[1]
    print(f"SCRATCH {_SCRATCH_DIR}", flush=True)
    print(f"READY {port}", flush=True)
    config = uvicorn.Config(app, fd=sock.fileno(), log_level="warning")
    server = uvicorn.Server(config)
    app.state.server = server
    server.run()


if __name__ == "__main__":
    raise SystemExit(main())
