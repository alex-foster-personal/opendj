"""Regression tests for PARITY-02's /anlz beatgrid-override seam.

The persisted-default / in-memory-toggle selection surface itself
(GET/PUT /api/v1/analysis/source, apps.analysis.selection) is comprehensively
covered by tests/analysis_contract/test_selection.py (PR #1549, issue #1476) --
this module does not duplicate that coverage. What that module does NOT
exercise is the one thing genuinely new here: rb_assets.py's beatgrid /anlz
payload overlay, which reads the resolved selection and swaps in the
apps.analysis grid for "own".

Builds a real, production-migrated state.db (apps.analysis.store.upsert_record,
same fixture shape as tests/test_analysis_route.py) with deliberately UNMAPPED
tracks, rebinds it onto apps.adapters.rekordbox.config.STATE_DB, and drives the
real GET /api/v1/tracks/{stable_id}/anlz route end to end -- so it needs no
real rekordbox data (data/master.plain.db / data/state/state.db, both
gitignored and absent in CI) while still exercising production code, not a
fabricated request (AGENTS.md:L55-L63).

Regression one-liners:
  - if a rekordbox-selected /anlz doesn't leave the served beatgrid alone then broken
  - if an own-selected /anlz with a real analysis record doesn't swap in the
    apps.analysis grid, exact ANLZ {n,bpm,t} shape then broken
  - if an own-selected /anlz with no analysis record silently keeps serving the
    rekordbox grid instead of an explicit empty grid + reason then broken
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.adapters.rekordbox import config as rb_config
from apps.analysis import selection as sel
from apps.analysis.record import AnalysisRecord
from apps.analysis.store import upsert_record
from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.routes.analysis import router as analysis_router
from apps.webui.server.routes.rb_assets import router as rb_assets_router

DURATION_S = 60.0
BPM = 120.0
BAR_S = 240.0 / BPM

SID_WITH_OWN = "sid-own-grid"
SID_NO_DOWNBEATS = "sid-no-downbeats"
SID_UNANALYZED = "sid-never-analyzed"


def _record(sid: str, *, downbeats: list[float] | None = None) -> AnalysisRecord:
    if downbeats is None:
        downbeats = [i * BAR_S for i in range(int(DURATION_S / BAR_S))]
    return AnalysisRecord(
        stable_id=sid,
        backend="librosa+madmom",
        backend_version="test-1.0",
        analyzed_at=datetime(2026, 9, 1, tzinfo=UTC),
        duration_s=DURATION_S,
        sample_rate=44100,
        bpm=BPM, bpm_confidence=0.9,
        key_camelot="8A", key_openkey="8m", key_confidence=0.9,
        energy=6,
        onsets_s=[],
        downbeats_s=downbeats,
        features_blob={"rms": [], "rms_hop": 512},
    )


@pytest.fixture(autouse=True)
def _clean_toggles() -> Iterator[None]:
    """Every test starts at the launch state and leaves it there.

    apps.analysis.selection's dev toggle is process-local (module-level, by
    design -- PARITY-02), not per-app, so a test that sets "own" for beatgrid
    leaks into the next test in this process unless reset both sides.
    """
    sel.reset_toggles()
    yield
    sel.reset_toggles()


# ----- _resolve_beatgrid_source, exercised through the real /anlz route ------
#
# AGENTS.md:L55-L63 (no mocks and locked real fixtures): these used to drive
# `_resolve_beatgrid_source` directly against `_FakeRequest`/`_FakeApp`/
# `_FakeAppState` duck-types -- fabricated application state that never
# touches FastAPI routing, dependency injection, or `get_track_anlz`'s own
# exception handling. Real production `/api/v1/tracks/{stable_id}/anlz` is
# hit instead, via a real, production-migrated state.db (`apps.analysis.store
# .open_conn` runs the same Phase 5 migrations `apps.shared.state.db.open_rw`
# does, then layers the analysis tables on top) rebound onto
# `apps.adapters.rekordbox.config.STATE_DB` -- the sanctioned override seam
# documented on that module ("Every constant here is a rebindable module
# attribute, on purpose").
#
# Every track here is deliberately UNMAPPED (no track_vendor_ids row), so
# `resolve_content` takes its real VENDOR_MAPPING_NOT_FOUND branch and the
# route serves `rb_vendor.local_anlz_payload` as its base payload -- this
# needs no data/master.plain.db (gitignored, absent in CI), matching this
# module's existing no-real-rekordbox-data contract. `file_path` points at a
# location that genuinely does not exist, so the waveform decode fails
# closed (AUDIO_FILE_MISSING -> LocalDecodeUnavailable -> `not_decoded`)
# rather than fabricating a decode.


_TS = "2026-09-01T00:00:00Z"


def _add_unmapped_track(db: Path, sid: str) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, artists_json, "
        "file_path, created_at, updated_at, deleted_at) VALUES (?,?,?,?,?,?,?,?)",
        (sid, "inferred", f"title-{sid}", "[]", str(db.parent / f"{sid}.flac"),
         _TS, _TS, None),
    )
    conn.commit()
    conn.close()


@pytest.fixture(scope="module")
def analysis_db(tmp_path_factory: pytest.TempPathFactory):
    db = tmp_path_factory.mktemp("analysis_source") / "state.db"
    upsert_record(_record(SID_WITH_OWN), db_path=db)
    upsert_record(_record(SID_NO_DOWNBEATS, downbeats=[]), db_path=db)
    return db


@pytest.fixture(scope="module")
def anlz_state_db(analysis_db: Path) -> Path:
    for sid in (SID_WITH_OWN, SID_NO_DOWNBEATS, SID_UNANALYZED):
        _add_unmapped_track(analysis_db, sid)
    return analysis_db


@pytest.fixture()
def anlz_client(
    anlz_state_db: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setattr(rb_config, "STATE_DB", anlz_state_db)
    app = FastAPI()
    app.state.backend = InMemoryBackend()
    app.state.analysis_db_path = anlz_state_db
    app.include_router(rb_assets_router, prefix="/api/v1")
    with TestClient(app) as test_client:
        yield test_client


def _set_source(client: TestClient, source: str) -> None:
    # "own"/"rekordbox" is this test module's own wire vocabulary (matching
    # /anlz's beatgrid_source field); apps.analysis.selection's dev toggle
    # speaks "own"/"rbx"/"unset" -- translate at the boundary, same as
    # rb_assets.py's _current_beatgrid_source does the other direction.
    del client  # the toggle is process-local, not client-scoped
    sel.set_toggle("beatgrid", "own" if source == "own" else "rbx")


@pytest.mark.requirement("PARITY-02")
def test_rekordbox_source_leaves_payload_beatgrid_untouched(anlz_client: TestClient) -> None:
    r = anlz_client.get(f"/api/v1/tracks/{SID_WITH_OWN}/anlz")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["beatgrid_source"] == "rekordbox"
    assert body["beatgrid_own_unavailable_reason"] is None
    # No rekordbox mapping exists for this track, so the rekordbox-owned
    # beatgrid is genuinely empty -- same shape empty_anlz_payload always
    # produces, left untouched by the source swap.
    assert body["beatgrid"] == {"beat_count": 0, "beats": []}


@pytest.mark.requirement("PARITY-02")
def test_own_source_swaps_in_the_analysis_grid_in_exact_anlz_shape(
    anlz_client: TestClient,
) -> None:
    _set_source(anlz_client, "own")
    r = anlz_client.get(f"/api/v1/tracks/{SID_WITH_OWN}/anlz")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["beatgrid_source"] == "own"
    assert body["beatgrid_own_unavailable_reason"] is None
    grid = body["beatgrid"]
    assert grid["beat_count"] == len(grid["beats"]) > 0
    for beat in grid["beats"]:
        assert set(beat) == {"n", "bpm", "t"}
        assert beat["bpm"] == pytest.approx(BPM, abs=0.01)


@pytest.mark.requirement("PARITY-02")
def test_own_source_with_no_analysis_record_goes_explicitly_empty_never_rekordbox(
    anlz_client: TestClient,
) -> None:
    _set_source(anlz_client, "own")
    r = anlz_client.get(f"/api/v1/tracks/{SID_UNANALYZED}/anlz")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["beatgrid_source"] == "own"
    assert body["beatgrid"] == {"beat_count": 0, "beats": []}
    assert body["beatgrid_own_unavailable_reason"] == "no own analysis for this track"


@pytest.mark.requirement("PARITY-02")
def test_own_source_with_downbeat_less_record_names_that_reason(
    anlz_client: TestClient,
) -> None:
    _set_source(anlz_client, "own")
    r = anlz_client.get(f"/api/v1/tracks/{SID_NO_DOWNBEATS}/anlz")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["beatgrid"] == {"beat_count": 0, "beats": []}
    assert body["beatgrid_own_unavailable_reason"] == "own analysis has no usable downbeats"


def test_analysis_router_still_wires_up_alongside_the_new_router(analysis_db) -> None:
    """Control: the pre-existing analysis router (auto-cues/beatgrid-fallback)
    imports and mounts fine next to the new module -- guards against an
    accidental circular import between routes.rb_assets and routes.analysis."""
    app = FastAPI()
    app.state.backend = InMemoryBackend()
    app.state.analysis_db_path = analysis_db
    app.state.anlz_available_fn = lambda _sid: False
    app.include_router(analysis_router, prefix="/api/v1")
    with TestClient(app) as client:
        r = client.get(f"/api/v1/tracks/{SID_WITH_OWN}/beatgrid-fallback")
    assert r.status_code == 200, r.text
