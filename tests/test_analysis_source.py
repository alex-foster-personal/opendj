"""Regression tests for PARITY-02 (in-app rbx-vs-own analysis source toggle).

Self-contained: the store/routes half needs nothing but a bare FastAPI app;
the /anlz beatgrid-override half builds a tmp state.db through the
documented writer path (apps.analysis.store.upsert_record), same fixture
shape as tests/test_analysis_route.py, and exercises the override helper
directly so it needs no real rekordbox data (data/master.plain.db /
data/state/state.db, both gitignored and absent in CI).

Regression one-liners:
  - if GET /analysis-source doesn't default every feature to rekordbox then broken
  - if PUT /analysis-source doesn't 404 ANALYSIS_SOURCE_FEATURE_NOT_FOUND for an
    unknown feature then broken
  - if PUT /analysis-source doesn't persist within the process AND a fresh
    store doesn't default back to rekordbox then broken
  - if a rekordbox-selected /anlz doesn't leave the served beatgrid alone then broken
  - if an own-selected /anlz with a real analysis record doesn't swap in the
    apps.analysis grid, exact ANLZ {n,bpm,t} shape then broken
  - if an own-selected /anlz with no analysis record silently keeps serving the
    rekordbox grid instead of an explicit empty grid + reason then broken
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.analysis.record import AnalysisRecord
from apps.analysis.store import upsert_record
from apps.webui.server.analysis_source import AnalysisSourceStore
from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.routes.analysis import router as analysis_router
from apps.webui.server.routes.analysis_source import router as analysis_source_router
from apps.webui.server.routes.rb_assets import _resolve_beatgrid_source

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


# ----- GET/PUT /api/v1/analysis-source ----------------------------------------


@pytest.fixture()
def source_client() -> Iterator[TestClient]:
    app = FastAPI()
    app.state.analysis_source = AnalysisSourceStore()
    app.include_router(analysis_source_router, prefix="/api/v1")
    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.requirement("PARITY-02")
def test_default_source_is_rekordbox(source_client: TestClient) -> None:
    r = source_client.get("/api/v1/analysis-source")
    assert r.status_code == 200, r.text
    assert r.json() == {"features": {"beatgrid": "rekordbox"}}


@pytest.mark.requirement("PARITY-02")
def test_put_switches_source_and_get_reflects_it(source_client: TestClient) -> None:
    r = source_client.put("/api/v1/analysis-source", json={"feature": "beatgrid", "source": "own"})
    assert r.status_code == 200, r.text
    assert r.json() == {"features": {"beatgrid": "own"}}
    assert source_client.get("/api/v1/analysis-source").json() == {"features": {"beatgrid": "own"}}


@pytest.mark.requirement("PARITY-02")
def test_put_unknown_feature_404s(source_client: TestClient) -> None:
    r = source_client.put("/api/v1/analysis-source", json={"feature": "vocals", "source": "own"})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "ANALYSIS_SOURCE_FEATURE_NOT_FOUND"
    # And the known feature's selection is untouched by the rejected call.
    unchanged = source_client.get("/api/v1/analysis-source").json()
    assert unchanged == {"features": {"beatgrid": "rekordbox"}}


@pytest.mark.requirement("PARITY-02")
def test_a_fresh_store_never_inherits_a_prior_selection() -> None:
    """Models a relaunch: a new AnalysisSourceStore is a new process's worth
    of state, so it must not remember a previous instance's 'own' pick."""
    first = AnalysisSourceStore()
    first.set("beatgrid", "own")
    assert first.get("beatgrid") == "own"

    second = AnalysisSourceStore()
    assert second.get("beatgrid") == "rekordbox"


# ----- _resolve_beatgrid_source (the /anlz value-swap) -------------------------


class _FakeAppState:
    def __init__(self, db_path, source: str) -> None:
        self.analysis_db_path = db_path
        self.analysis_source = AnalysisSourceStore()
        self.analysis_source.set("beatgrid", source)


class _FakeApp:
    def __init__(self, db_path, source: str) -> None:
        self.state = _FakeAppState(db_path, source)


class _FakeRequest:
    """Duck-types the ``request.app.state.X`` reads _resolve_beatgrid_source
    and analysis._analysis_db_path need -- no real HTTP layer required."""

    def __init__(self, db_path, source: str) -> None:
        self.app = _FakeApp(db_path, source)


@pytest.fixture(scope="module")
def analysis_db(tmp_path_factory: pytest.TempPathFactory):
    db = tmp_path_factory.mktemp("analysis_source") / "state.db"
    upsert_record(_record(SID_WITH_OWN), db_path=db)
    upsert_record(_record(SID_NO_DOWNBEATS, downbeats=[]), db_path=db)
    return db


@pytest.mark.requirement("PARITY-02")
def test_rekordbox_source_leaves_payload_beatgrid_untouched(analysis_db) -> None:
    request = _FakeRequest(analysis_db, "rekordbox")
    payload = {"beatgrid": {"beat_count": 1, "beats": [{"n": 1, "bpm": BPM, "t": 0.0}]}}
    _resolve_beatgrid_source(request, SID_WITH_OWN, payload)
    assert payload["beatgrid_source"] == "rekordbox"
    assert payload["beatgrid_own_unavailable_reason"] is None
    assert payload["beatgrid"] == {"beat_count": 1, "beats": [{"n": 1, "bpm": BPM, "t": 0.0}]}


@pytest.mark.requirement("PARITY-02")
def test_own_source_swaps_in_the_analysis_grid_in_exact_anlz_shape(analysis_db) -> None:
    request = _FakeRequest(analysis_db, "own")
    payload = {"beatgrid": {"beat_count": 1, "beats": [{"n": 1, "bpm": 999.0, "t": 0.0}]}}
    _resolve_beatgrid_source(request, SID_WITH_OWN, payload)
    assert payload["beatgrid_source"] == "own"
    assert payload["beatgrid_own_unavailable_reason"] is None
    grid = payload["beatgrid"]
    assert grid["beat_count"] == len(grid["beats"]) > 0
    for beat in grid["beats"]:
        assert set(beat) == {"n", "bpm", "t"}
        assert beat["bpm"] == pytest.approx(BPM, abs=0.01)
    # The rekordbox value that was in the payload before the swap is gone.
    assert 999.0 not in {b["bpm"] for b in grid["beats"]}


@pytest.mark.requirement("PARITY-02")
def test_own_source_with_no_analysis_record_goes_explicitly_empty_never_rekordbox(
    analysis_db,
) -> None:
    request = _FakeRequest(analysis_db, "own")
    payload = {"beatgrid": {"beat_count": 4, "beats": [{"n": 1, "bpm": BPM, "t": 0.0}] * 4}}
    _resolve_beatgrid_source(request, "sid-totally-unknown", payload)
    assert payload["beatgrid_source"] == "own"
    assert payload["beatgrid"] == {"beat_count": 0, "beats": []}
    assert payload["beatgrid_own_unavailable_reason"] == "no own analysis for this track"


@pytest.mark.requirement("PARITY-02")
def test_no_analysis_source_on_app_state_defaults_to_rekordbox(analysis_db) -> None:
    """A test app that mounts only the rb-assets router (test_rb_assets.py's
    own fixture shape) never sets app.state.analysis_source. That must not
    AttributeError -- it is the same 'test app is a subset of the real one'
    convention routes.analysis already honors for analysis_db_path."""

    class _BareState:
        pass

    class _BareApp:
        state = _BareState()

    class _BareRequest:
        app = _BareApp()

    payload = {"beatgrid": {"beat_count": 1, "beats": [{"n": 1, "bpm": BPM, "t": 0.0}]}}
    _resolve_beatgrid_source(_BareRequest(), SID_WITH_OWN, payload)
    assert payload["beatgrid_source"] == "rekordbox"
    assert payload["beatgrid"] == {"beat_count": 1, "beats": [{"n": 1, "bpm": BPM, "t": 0.0}]}


@pytest.mark.requirement("PARITY-02")
def test_own_source_with_downbeat_less_record_names_that_reason(analysis_db) -> None:
    request = _FakeRequest(analysis_db, "own")
    payload = {"beatgrid": {"beat_count": 1, "beats": [{"n": 1, "bpm": BPM, "t": 0.0}]}}
    _resolve_beatgrid_source(request, SID_NO_DOWNBEATS, payload)
    assert payload["beatgrid"] == {"beat_count": 0, "beats": []}
    assert payload["beatgrid_own_unavailable_reason"] == "own analysis has no usable downbeats"


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
