"""Regression tests for the analysis router (auto-cues + beatgrid-fallback).

Self-contained: builds a tmp state.db through the documented writer path
(apps.analysis.store.upsert_record) and injects it via
``app.state.analysis_db_path``; the ANLZ disk probe is injected via
``app.state.anlz_available_fn``. No real library data touched.

Regression one-liners:
  - if /auto-cues doesn't 200 with proposal:true + [{time_s,kind,confidence,source}] then broken
  - if /auto-cues kinds leave the intro/drop/break/outro/'' set or exceed 8 cues then broken
  - if /auto-cues isn't deterministic across identical calls then broken
  - if /auto-cues doesn't 404 ANALYSIS_NOT_FOUND for an unknown stable_id then broken
  - if /auto-cues ?backend=<unknown> doesn't 404 ANALYSIS_NOT_FOUND then broken
  - if /auto-cues 404s for a library track with no analysis row then unanalyzed loads break
  - if /beatgrid-fallback beats aren't exactly fallback-shaped {n,bpm,t,extrapolated} then broken
  - if /beatgrid-fallback n doesn't read 1 on every analysis downbeat then broken
  - if /beatgrid-fallback beat times aren't strictly increasing within duration then broken
  - if /beatgrid-fallback doesn't 404 BEATGRID_FALLBACK_NOT_FOUND when neither source exists then broken
  - if /beatgrid-fallback 404 detail doesn't say anlz_available:true when ANLZ exists then broken
  - if a record without downbeats yields an invented grid instead of a 404 then broken
  - if ?backend selection or newest-row default pick the wrong analysis row then broken
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.analysis.lane_enums import LaneStatus
from apps.analysis.lanes import LaneResult
from apps.analysis.record import AnalysisRecord
from apps.analysis.store import upsert_record
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend, Track
from apps.webui.server.routes.analysis import (
    AUTO_CUES_SOURCE,
    BEATGRID_SOURCE,
    router,
    synthesize_fallback_beats,
)

DURATION_S = 60.0
BPM = 120.0            # -> 2.0 s bars, downbeats at 0, 2, 4, ...
BAR_S = 240.0 / BPM

SID_FULL = "sid-full"              # onsets + downbeats + rms -> cues + grid
SID_NO_DOWNBEATS = "sid-nodown"    # analysis row but no downbeats
SID_ANLZ_ONLY = "sid-anlz-only"    # no analysis row; ANLZ present on disk
SID_UNKNOWN = "sid-unknown"        # nothing anywhere
SID_UNANALYZED = "sid-unanalyzed"  # library row, no analysis
# A legacy librosa row AND a v1 own_beatgrid row that FAILED - v1 has already
# settled a beatgrid determination for this track, so the endpoint must not
# fall through to the superseded legacy row (discussion_r3975326241).
SID_OWN_SETTLED_FAILED = "sid-own-settled-failed"
SID_ISSUE_1777 = "sid-issue-1777"
# sha256 shape required by AnalysisRecord's own-record contract; content is
# arbitrary, only the shape is checked.
_DECODE_FINGERPRINT = "sha256:" + "ab" * 32

ANLZ_PRESENT = {SID_ANLZ_ONLY}

ALLOWED_KINDS = {"intro", "drop", "break", "outro", ""}


def _record(
    sid: str,
    *,
    backend: str = "librosa+madmom",
    backend_version: str = "test-1.0",
    analyzed_at: datetime = datetime(2026, 4, 17, tzinfo=UTC),
    downbeats: list[float] | None = None,
    bpm: float = BPM,
    duration_s: float = DURATION_S,
) -> AnalysisRecord:
    if downbeats is None:
        downbeats = [i * BAR_S for i in range(int(duration_s / BAR_S))]
    onsets = [round(0.25 + i * 1.9, 3) for i in range(30)]
    # rms envelope: quiet, loud spike mid-track (a "drop"), quiet again.
    rms = [0.1] * 200 + [0.9] * 40 + [0.1] * 200
    return AnalysisRecord(
        stable_id=sid,
        backend=backend,
        backend_version=backend_version,
        analyzed_at=analyzed_at,
        duration_s=duration_s,
        sample_rate=44100,
        bpm=bpm, bpm_confidence=0.9,
        key_camelot="8A", key_openkey="8m", key_confidence=0.9,
        energy=6,
        onsets_s=onsets,
        downbeats_s=downbeats,
        features_blob={"rms": rms, "rms_hop": 512},
    )


def _own_beatgrid_record(
    sid: str, *, status: LaneStatus, reason: str | None
) -> AnalysisRecord:
    """A v1 own_beatgrid row that made a real determination (ok or failed).

    Only the fields the record contract actually checks are filled in: see
    ``_check_provenance_and_lanes`` (apps/analysis/record.py).
    """
    return AnalysisRecord(
        stable_id=sid,
        backend="own_beatgrid.inapp",
        backend_version="1.0.0",
        analyzed_at=datetime(2026, 9, 9, tzinfo=UTC),
        duration_s=DURATION_S,
        sample_rate=44100,
        bpm=0.0, bpm_confidence=0.0,
        key_camelot="8A", key_openkey="8m", key_confidence=0.0,
        energy=0,
        producer="inapp",
        producer_version="1.0.0",
        decode_fingerprint=_DECODE_FINGERPRINT,
        lanes={"beatgrid": LaneResult(status=status, reason=reason)},
    )


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    db = tmp_path_factory.mktemp("analysis_route") / "state.db"
    upsert_record(_record(SID_FULL), db_path=db)
    upsert_record(
        _record(
            SID_ISSUE_1777,
            downbeats=[0.5, 2.375, 4.25, 6.125],
            bpm=128.0,
            duration_s=360.0,
        ),
        db_path=db,
    )
    upsert_record(_record(SID_NO_DOWNBEATS, downbeats=[]), db_path=db)
    # A pre-v1 legacy row that predates the own_beatgrid canonical pointer,
    # PLUS a v1 own_beatgrid row that ran and FAILED - the fallback endpoint
    # must defer to v1's settled "no grid" answer, not the older legacy row.
    upsert_record(_record(SID_OWN_SETTLED_FAILED), db_path=db)
    upsert_record(
        _own_beatgrid_record(
            SID_OWN_SETTLED_FAILED, status="failed", reason="no_trackable_pulse"
        ),
        db_path=db,
    )
    # Two backends for SID_FULL: an OLDER row from another backend. The
    # default (no ?backend) must pick the newest analyzed_at row.
    upsert_record(
        _record(
            SID_FULL,
            backend="mik",
            backend_version="old-0.1",
            analyzed_at=datetime(2026, 1, 1, tzinfo=UTC),
            bpm=119.0,
        ),
        db_path=db,
    )

    app = FastAPI()
    mem = InMemoryBackend()
    mem.seed_track(Track(stable_id=SID_UNANALYZED))
    app.state.backend = mem
    app.state.analysis_db_path = db
    app.state.anlz_available_fn = lambda sid: sid in ANLZ_PRESENT
    app.include_router(router, prefix="/api/v1")
    with TestClient(app) as test_client:
        yield test_client


# ----- /auto-cues -------------------------------------------------------------

@pytest.mark.requirement("META-04")
def test_auto_cues_contract_shape(client: TestClient) -> None:
    r = client.get(f"/api/v1/tracks/{SID_FULL}/auto-cues")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["proposal"] is True
    assert body["stable_id"] == SID_FULL
    assert body["source"] == AUTO_CUES_SOURCE
    assert body["backend"] == "librosa+madmom"
    assert body["backend_version"] == "test-1.0"
    assert body["proposals"], "expected at least one proposed cue"
    assert len(body["proposals"]) <= 8
    times = [p["time_s"] for p in body["proposals"]]
    assert times == sorted(times)
    for p in body["proposals"]:
        assert set(p) == {"time_s", "kind", "confidence", "source", "rms_dbfs"}
        assert p["kind"] in ALLOWED_KINDS
        assert 0.0 <= p["confidence"] <= 1.0
        assert 0.0 <= p["time_s"] <= DURATION_S
        assert p["source"] == AUTO_CUES_SOURCE


@pytest.mark.requirement("META-04")
def test_auto_cues_deterministic(client: TestClient) -> None:
    a = client.get(f"/api/v1/tracks/{SID_FULL}/auto-cues").json()
    b = client.get(f"/api/v1/tracks/{SID_FULL}/auto-cues").json()
    assert a == b


@pytest.mark.requirement("META-04")
def test_auto_cues_unknown_sid_404(client: TestClient) -> None:
    r = client.get(f"/api/v1/tracks/{SID_UNKNOWN}/auto-cues")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "ANALYSIS_NOT_FOUND"


@pytest.mark.requirement("META-04")
def test_auto_cues_unknown_backend_404(client: TestClient) -> None:
    r = client.get(f"/api/v1/tracks/{SID_FULL}/auto-cues?backend=bogus")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "ANALYSIS_NOT_FOUND"


@pytest.mark.requirement("META-04")
def test_auto_cues_unanalyzed_library_track_is_200_empty(client: TestClient) -> None:
    r = client.get(f"/api/v1/tracks/{SID_UNANALYZED}/auto-cues")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["proposal"] is True
    assert body["stable_id"] == SID_UNANALYZED
    assert body["source"] == AUTO_CUES_SOURCE
    assert body["backend"] == "none"
    assert body["backend_version"] == "none"
    assert body["proposals"] == []


@pytest.mark.requirement("META-04")
def test_auto_cues_backend_param_selects_row(client: TestClient) -> None:
    r = client.get(f"/api/v1/tracks/{SID_FULL}/auto-cues?backend=mik")
    assert r.status_code == 200
    assert r.json()["backend"] == "mik"
    assert r.json()["backend_version"] == "old-0.1"


@pytest.mark.requirement("META-04")
def test_production_app_wires_configured_db_for_analysis_routes(
    tmp_path: Path,
) -> None:
    """The production app factory must bind analysis reads to its state DB."""
    configured_db = tmp_path / "configured-data" / "state" / "state.db"
    upsert_record(_record(SID_FULL), db_path=configured_db)

    app = create_app(
        backend=InMemoryBackend(),
        state_db_path=str(configured_db),
        mount_frontend=False,
        port=18698,
        frontend_port=19412,
    )
    app.state.anlz_available_fn = lambda _sid: False

    with TestClient(app) as production_client:
        auto_cues = production_client.get(
            f"/api/v1/tracks/{SID_FULL}/auto-cues"
        )
        beatgrid = production_client.get(
            f"/api/v1/tracks/{SID_FULL}/beatgrid-fallback"
        )

    assert auto_cues.status_code == 200, auto_cues.text
    assert beatgrid.status_code == 200, beatgrid.text


# ----- /beatgrid-fallback -----------------------------------------------------

@pytest.mark.requirement("META-02")
def test_beatgrid_fallback_matches_anlz_shape(client: TestClient) -> None:
    r = client.get(f"/api/v1/tracks/{SID_FULL}/beatgrid-fallback")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == BEATGRID_SOURCE
    assert body["backend"] == "librosa+madmom"   # newest row wins
    assert body["bpm"] == BPM
    assert body["anlz_available"] is False
    grid = body["beatgrid"]
    assert set(grid) == {"source", "status", "beat_count", "beats"}
    # Required on the wire (AnlzBeatgridSource in anlz-types.ts): a fail-closed,
    # source-aware reader rejects a grid whose source it cannot name (Codex P2
    # BLOCKING, PR #1587).
    assert grid["source"] == "own"
    assert grid["status"] == "ok"
    assert grid["beat_count"] == len(grid["beats"]) > 0
    for beat in grid["beats"]:
        # Fallback beats carry extrapolated; PQTZ /anlz stays {n,bpm,t} only.
        assert set(beat) == {"n", "bpm", "t", "extrapolated"}
        assert beat["n"] in (1, 2, 3, 4)
        assert 0.0 <= beat["t"] <= DURATION_S
        assert beat["bpm"] == pytest.approx(BPM, abs=0.01)
    times = [b["t"] for b in grid["beats"]]
    assert times == sorted(times) and len(set(times)) == len(times)


@pytest.mark.requirement("META-02")
def test_beatgrid_fallback_downbeats_are_bar_starts(client: TestClient) -> None:
    grid = client.get(
        f"/api/v1/tracks/{SID_FULL}/beatgrid-fallback"
    ).json()["beatgrid"]
    n1_times = {b["t"] for b in grid["beats"] if b["n"] == 1}
    for downbeat in (0.0, 2.0, 4.0, 58.0):
        assert downbeat in n1_times


@pytest.mark.requirement("META-02")
def test_beatgrid_fallback_defers_to_a_settled_own_lane(client: TestClient) -> None:
    """A v1 own_beatgrid row that FAILED must win over an older legacy row.

    r3975326241 P1 BLOCKING: without this guard, `_load_latest_record`'s
    own_* exclusion made the newest NON-own row win regardless of whether
    v1 had already made its own (failed) determination, so a stale
    pre-v1 grid would be served under the exact selection state ("own",
    no usable grid) that the canonical pointer says has none.
    """
    r = client.get(f"/api/v1/tracks/{SID_OWN_SETTLED_FAILED}/beatgrid-fallback")
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["code"] == "BEATGRID_FALLBACK_NOT_FOUND"


@pytest.mark.requirement("META-02")
def test_beatgrid_fallback_explicit_backend_bypasses_the_settled_gate(
    client: TestClient,
) -> None:
    """A caller naming ``?backend=`` gets exactly what it named, settled or not."""
    r = client.get(
        f"/api/v1/tracks/{SID_OWN_SETTLED_FAILED}/beatgrid-fallback",
        params={"backend": "librosa+madmom"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["backend"] == "librosa+madmom"


@pytest.mark.requirement("META-02")
def test_beatgrid_fallback_404_when_neither_exists(client: TestClient) -> None:
    r = client.get(f"/api/v1/tracks/{SID_UNKNOWN}/beatgrid-fallback")
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "BEATGRID_FALLBACK_NOT_FOUND"
    assert detail["anlz_available"] is False


@pytest.mark.requirement("META-02")
def test_beatgrid_fallback_404_points_at_anlz_when_present(client: TestClient) -> None:
    r = client.get(f"/api/v1/tracks/{SID_ANLZ_ONLY}/beatgrid-fallback")
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert detail["code"] == "BEATGRID_FALLBACK_NOT_FOUND"
    assert detail["anlz_available"] is True
    assert "/anlz" in detail["message"]


@pytest.mark.requirement("META-02")
def test_beatgrid_fallback_never_invented_without_downbeats(client: TestClient) -> None:
    r = client.get(f"/api/v1/tracks/{SID_NO_DOWNBEATS}/beatgrid-fallback")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "BEATGRID_FALLBACK_NOT_FOUND"


# ----- synthesize_fallback_beats unit edges -----------------------------------

def test_synthesize_fallback_beats_returns_none_without_downbeats() -> None:
    """[if] synthesize_fallback_beats has no downbeats [then] it returns None."""
    rec = _record("empty", downbeats=[])
    assert synthesize_fallback_beats(rec) is None


@pytest.mark.requirement("META-02")
def test_synthesize_single_downbeat_extends_at_record_bpm() -> None:
    rec = _record("solo", downbeats=[1.0])
    beats = synthesize_fallback_beats(rec)
    assert beats is not None
    assert beats[0].t == 1.0 and beats[0].n == 1 and beats[0].extrapolated is False
    # 120 BPM -> 0.5 s beats from t=1.0 to duration.
    assert beats[1].t == pytest.approx(1.5)
    assert beats[1].extrapolated is True
    assert all(b.t < DURATION_S for b in beats)
    assert all(b.extrapolated for b in beats[1:])
    ns = [b.n for b in beats[:8]]
    assert ns == [1, 2, 3, 4, 1, 2, 3, 4]


@pytest.mark.requirement("PARITY-10")
def test_synthesize_fallback_marks_extrapolated_tail() -> None:
    """Issue #1777: tail beats past the last detected downbeat are marked."""
    downbeats = [0.5, 2.375, 4.25, 6.125]
    rec = replace(_record("issue-1777", downbeats=downbeats, bpm=128.0), duration_s=360.0)
    beats = synthesize_fallback_beats(rec)
    assert beats is not None
    assert len(beats) == 767
    last_measured = [b for b in beats if b.t == pytest.approx(6.125)]
    assert len(last_measured) == 1
    assert last_measured[0].n == 1
    assert last_measured[0].extrapolated is False
    assert all(b.extrapolated for b in beats if b.t > 6.125)
    assert all(not b.extrapolated for b in beats if b.t <= 6.125)
    assert sum(1 for b in beats if b.extrapolated) == 754
    assert sum(1 for b in beats if b.n == 1 and not b.extrapolated) == 4


@pytest.mark.requirement("PARITY-10")
def test_beatgrid_fallback_http_marks_extrapolated_tail(client: TestClient) -> None:
    r = client.get(f"/api/v1/tracks/{SID_ISSUE_1777}/beatgrid-fallback")
    assert r.status_code == 200, r.text
    beats = r.json()["beatgrid"]["beats"]
    assert len(beats) == 767
    for beat in beats:
        assert set(beat) == {"n", "bpm", "t", "extrapolated"}
    assert all(b["extrapolated"] for b in beats if b["t"] > 6.125)
    assert all(not b["extrapolated"] for b in beats if b["t"] <= 6.125)
    assert sum(1 for b in beats if b["extrapolated"]) == 754


@pytest.mark.requirement("META-02")
def test_synthesize_non_monotonic_downbeats_fail_fast() -> None:
    from fastapi import HTTPException

    rec = _record("corrupt", downbeats=[0.0, 2.0, 1.5])
    with pytest.raises(HTTPException) as exc_info:
        synthesize_fallback_beats(rec)
    assert exc_info.value.status_code == 500
    assert exc_info.value.detail["code"] == "ANALYSIS_RECORD_INVALID"
