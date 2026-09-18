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

FIXTURE PROVENANCE (changed Wed 9 Sep 2026, discussion_r3969942709). These
tracks used to be seeded with a LEGACY ``librosa+madmom`` record, which the
old own branch picked up through ``_load_latest_record(.., None)``. That query
excludes every ``own_*`` backend by design, so the fixture could never have
exercised a native own record and the test passed on a legacy grid the read
model does not use. They now seed a real ``own_beatgrid.inapp`` record through
``apps.analysis.store.upsert_record``, which builds the same
``analysis_canonical`` pointer production builds, PLUS a newer legacy row that
must lose. Acceptance impact: the own-grid assertions got strictly stronger --
the old fixture could not distinguish the two records at all.

Regression one-liners:
  - if a rekordbox-selected /anlz doesn't leave the served beatgrid alone then broken
  - if an own-selected /anlz doesn't serve the CANONICAL own_beatgrid record's
    own beats, exact ANLZ {n,bpm,t} shape then broken
  - if a newer legacy (non-own) analysis row outranks the canonical own record
    for an own-selected /anlz then broken
  - if an own-selected /anlz with no analysis record silently keeps serving the
    rekordbox grid instead of an explicit empty grid + reason then broken
  - if an own beatgrid lane that FAILED is served as an empty grid without the
    lane's own named reason then broken
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
from apps.analysis.lanes import LaneResult
from apps.analysis.record import AnalysisRecord
from apps.analysis.store import upsert_record
from apps.webui.server.app import create_app
from apps.webui.server.backend import InMemoryBackend
from apps.webui.server.routes.analysis import router as analysis_router
from apps.webui.server.routes.rb_assets import _resolve_beatgrid_source
from tests.analysis_contract.conftest import beatgrid_payload, own_record

DURATION_S = 60.0
BPM = 120.0
BAR_S = 240.0 / BPM

SID_WITH_OWN = "sid-own-grid"
SID_OWN_LANE_FAILED = "sid-own-lane-failed"
SID_UNANALYZED = "sid-never-analyzed"

#: The own beatgrid fixture's tempo, and the number of beats it lays down.
#: Both are read back off the served payload, so a producer change that moved
#: the grid would fail here rather than silently redefine the expectation.
OWN_BPM = 128.0


def _record(
    sid: str,
    *,
    downbeats: list[float] | None = None,
    analyzed_at: datetime = datetime(2026, 9, 1, tzinfo=UTC),
) -> AnalysisRecord:
    """A LEGACY (non-own) analysis row -- the kind the canonical pointer skips."""
    if downbeats is None:
        downbeats = [i * BAR_S for i in range(int(DURATION_S / BAR_S))]
    return AnalysisRecord(
        stable_id=sid,
        backend="librosa+madmom",
        backend_version="test-1.0",
        analyzed_at=analyzed_at,
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
    """A real state.db carrying BOTH record kinds for the own-grid track.

    The legacy row is analyzed LATER than the own one on purpose: it is what
    ``_load_latest_record(.., None)`` would return, so any regression back to
    "newest non-own row wins" serves a 120 BPM legacy grid here instead of the
    canonical 128 BPM own grid and the shape assertion fails.
    """
    db = tmp_path_factory.mktemp("analysis_source") / "state.db"
    upsert_record(
        own_record(stable_id=SID_WITH_OWN, result=LaneResult(
            status="ok", payload=beatgrid_payload(bpm=OWN_BPM),
        )),
        db_path=db,
    )
    upsert_record(_record(SID_WITH_OWN, analyzed_at=datetime(2026, 9, 8, tzinfo=UTC)), db_path=db)
    upsert_record(
        own_record(stable_id=SID_OWN_LANE_FAILED, result=LaneResult(
            status="failed", reason="no_trackable_pulse",
        )),
        db_path=db,
    )
    return db


@pytest.fixture(scope="module")
def anlz_state_db(analysis_db: Path) -> Path:
    for sid in (SID_WITH_OWN, SID_OWN_LANE_FAILED, SID_UNANALYZED):
        _add_unmapped_track(analysis_db, sid)
    return analysis_db


@pytest.fixture()
def anlz_client(
    anlz_state_db: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """The PRODUCTION app factory, not a partial hand-built one.

    ``create_app`` is what actually binds ``state_db_path`` onto
    ``app.state.analysis_db_path`` (apps/webui/server/app.py's own boot path
    does the same thing for the real daemon), so building the app any other
    way would let this test pass even if that binding broke in production
    (discussion_r3972682737 P1 BLOCKING). ``rb_config.STATE_DB`` still needs
    its own rebind: ``create_app`` never touches it, and the rekordbox
    adapter's vendor-mapping lookups (apps/adapters/rekordbox/paths.py) read
    it directly rather than through ``app.state`` -- this is the sanctioned
    per-test override seam documented on ``apps.adapters.rekordbox.config``
    itself ("Every constant here is a rebindable module attribute, on
    purpose"), not the fabricated application state AGENTS.md:L125-L133
    prohibits.
    """
    monkeypatch.setattr(rb_config, "STATE_DB", anlz_state_db)
    app = create_app(
        backend=InMemoryBackend(),
        state_db_path=str(anlz_state_db),
        mount_frontend=False,
        port=18712,
        frontend_port=19414,
    )
    with TestClient(app) as test_client:
        yield test_client


def _set_source(client: TestClient, source: str) -> None:
    """Drive the real PUT /api/v1/analysis/source endpoint.

    Calling ``sel.set_toggle`` directly exercised the selection module but
    never the HTTP route wiring in front of it, so a break in that route
    (bad dependency, wrong lane translation, a broken write guard) could not
    fail this test even though this module's whole point is the overlay's
    end-to-end behavior (discussion_r3972682737 P1 BLOCKING). Callers already
    pass "own" or "rbx", the same vocabulary the endpoint's `toggle` field
    expects, so no translation is needed here.
    """
    r = client.put("/api/v1/analysis/source", json={"lane": "beatgrid", "toggle": source})
    assert r.status_code == 200, r.text


def test_resolve_beatgrid_source_takes_its_source_as_a_plain_argument_not_a_second_read(
    analysis_db: Path,
) -> None:
    """discussion_r3974235458 P2 BLOCKING: `_resolve_beatgrid_source` used to
    take `request` and re-derive the selection itself, a SECOND read
    independent of the caller's own `_current_beatgrid_source` call for the
    ANALYSIS_NOT_FOUND rescue branch - an agent's PUT landing between the two
    reads could pair a rescue built for one source with a stamp for the
    other, e.g. a permanent lying `beatgrid_source: "rekordbox"` 200 for a
    track whose rekordbox ANLZ was genuinely missing.

    Now it takes `source: str` directly, so the caller's single snapshot is
    the only source of truth by construction - there is no `request` left to
    re-read from. This calls it directly with each source against the SAME
    real db and stable_id, proving the answer is a deterministic function of
    the argument alone, never of some independent live read.
    """
    payload_own: dict = {}
    _resolve_beatgrid_source("own", analysis_db, SID_WITH_OWN, payload_own)
    assert payload_own["beatgrid_source"] == "own"
    assert payload_own["beatgrid"]["beat_count"] > 0

    payload_rekordbox: dict = {}
    _resolve_beatgrid_source("rekordbox", analysis_db, SID_WITH_OWN, payload_rekordbox)
    assert payload_rekordbox["beatgrid_source"] == "rekordbox"
    assert payload_rekordbox["beatgrid_own_unavailable_reason"] is None


@pytest.mark.requirement("PARITY-02")
def test_rekordbox_source_leaves_payload_beatgrid_untouched(anlz_client: TestClient) -> None:
    """
    [if] rekordbox is selected [then] the served beatgrid is left untouched, [else stop].
    """
    r = anlz_client.get(f"/api/v1/tracks/{SID_WITH_OWN}/anlz")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["beatgrid_source"] == "rekordbox"
    assert body["beatgrid_own_unavailable_reason"] is None
    # No rekordbox mapping exists for this track, so the rekordbox-owned
    # beatgrid is genuinely empty -- same shape empty_anlz_payload always
    # produces, left untouched by the source swap. `source: "rekordbox"` is
    # the REQUIRED wire discriminator every beatgrid block now carries
    # (own_beatgrid_overlay.py's `_beatgrid_payload`/PR #1587), not something
    # this route's own source swap adds.
    assert body["beatgrid"] == {"source": "rekordbox", "beat_count": 0, "beats": []}


@pytest.mark.requirement("PARITY-02")
def test_own_source_swaps_in_the_analysis_grid_in_exact_anlz_shape(
    anlz_client: TestClient,
) -> None:
    """
    [if] the own lane has a real record [then] its beatgrid fills in exact ANLZ shape, [else stop].
    """
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
    # OWN_BPM, not BPM. The legacy librosa row seeded for this same track is
    # NEWER and carries 120, so reading the newest row instead of the
    # analysis_canonical pointer serves 120 here (discussion_r3969942709).
    assert sorted({beat["bpm"] for beat in grid["beats"]}) == [pytest.approx(OWN_BPM, abs=0.01)]
    assert grid["beats"] == sorted(grid["beats"], key=lambda beat: beat["t"])
    assert [beat["n"] for beat in grid["beats"][:5]] == [1, 2, 3, 4, 1]


@pytest.mark.requirement("PARITY-02")
def test_own_source_serves_the_same_record_the_projection_derives_bpm_from(
    anlz_state_db: Path, anlz_client: TestClient,
) -> None:
    """
    [if] the own grid and projection's bpm come from different records [then] fail, [else stop].
    """
    _set_source(anlz_client, "own")
    grid = anlz_client.get(f"/api/v1/tracks/{SID_WITH_OWN}/anlz").json()["beatgrid"]
    conn = sqlite3.connect(anlz_state_db)
    try:
        projected = conn.execute(
            "SELECT value FROM analysis_projection WHERE stable_id = ? AND field = 'bpm'",
            (SID_WITH_OWN,),
        ).fetchone()
    finally:
        conn.close()
    assert projected is not None, "the own record must project a bpm to compare against"
    # GET /tracks/{id} answers with this projected value once the beatgrid
    # lane is on own (analysis_overlay.lane_owned_fields). A grid whose beats
    # disagree with it is a read model reporting a tempo and a grid that were
    # never measured together.
    assert sorted({beat["bpm"] for beat in grid["beats"]}) == [
        pytest.approx(projected[0], abs=0.01)
    ]


@pytest.mark.requirement("PARITY-02")
def test_own_source_with_no_analysis_record_goes_explicitly_empty_never_rekordbox(
    anlz_client: TestClient,
) -> None:
    """
    [if] own has no analysis record [then] the beatgrid goes empty with a named reason, [else stop].
    """
    _set_source(anlz_client, "own")
    r = anlz_client.get(f"/api/v1/tracks/{SID_UNANALYZED}/anlz")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["beatgrid_source"] == "own"
    assert body["beatgrid"] == {
        "source": "own",
        "status": "missing",
        "reason": "no own beatgrid record for this track yet",
        "beat_count": 0,
        "beats": [],
    }
    assert body["beatgrid_own_unavailable_reason"] == "no own beatgrid record for this track yet"


@pytest.mark.requirement("PARITY-02")
def test_own_source_with_a_failed_beatgrid_lane_names_that_lanes_reason(
    anlz_client: TestClient,
) -> None:
    """
    [if] the own beatgrid lane failed [then] the grid empties with that lane's reason, [else stop].
    """
    _set_source(anlz_client, "own")
    r = anlz_client.get(f"/api/v1/tracks/{SID_OWN_LANE_FAILED}/anlz")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["beatgrid_source"] == "own"
    assert body["beatgrid"] == {
        "source": "own",
        "status": "failed",
        "reason": "no_trackable_pulse",
        "beat_count": 0,
        "beats": [],
    }
    # The LANE's own reason travels through verbatim, not a reason this route
    # invents: the same string analysis_projection carries for the failed
    # field, so the grid and the read model explain the gap identically.
    assert body["beatgrid_own_unavailable_reason"] == "no_trackable_pulse"


def test_a_source_switch_invalidates_the_anlz_validator_so_no_cache_can_hold_it(
    anlz_client: TestClient,
) -> None:
    """The /anlz URL carries no source, and the toggle is process-local, so the
    ONLY thing that can stop a cache serving a pre-switch grid is a validator
    that moves with the source (discussion_r3970967302)."""
    _set_source(anlz_client, "rbx")
    rbx = anlz_client.get(f"/api/v1/tracks/{SID_WITH_OWN}/anlz")
    assert rbx.status_code == 200
    assert rbx.headers["cache-control"] == "private, no-cache"
    rbx_etag = rbx.headers["etag"]

    # Unchanged source, unchanged answer: the bytes stay off the wire.
    assert (
        anlz_client.get(
            f"/api/v1/tracks/{SID_WITH_OWN}/anlz", headers={"If-None-Match": rbx_etag}
        ).status_code
        == 304
    )

    _set_source(anlz_client, "own")
    after = anlz_client.get(
        f"/api/v1/tracks/{SID_WITH_OWN}/anlz", headers={"If-None-Match": rbx_etag}
    )
    assert after.status_code == 200, "a stale rbx validator must NOT satisfy an own request"
    assert after.headers["etag"] != rbx_etag
    assert after.json()["beatgrid_source"] == "own"
    assert after.json()["beatgrid"]["beats"][0]["bpm"] == pytest.approx(OWN_BPM, abs=0.01)


def test_analysis_router_still_wires_up_alongside_the_new_router(analysis_db) -> None:
    """Control: the pre-existing analysis router (auto-cues/beatgrid-fallback)
    imports and mounts fine next to the new module -- guards against an
    accidental circular import between routes.rb_assets and routes.analysis.

    SID_WITH_OWN carries a successful own_beatgrid canonical pointer, so the
    default /beatgrid-fallback still serves the legacy row while own is not
    the serving source (STANDALONE-04). A 200 with the legacy backend proves
    the router executed end to end, which is this test's whole point."""
    app = FastAPI()
    app.state.backend = InMemoryBackend()
    app.state.analysis_db_path = analysis_db
    app.state.anlz_available_fn = lambda _sid: False
    app.include_router(analysis_router, prefix="/api/v1")
    with TestClient(app) as client:
        r = client.get(f"/api/v1/tracks/{SID_WITH_OWN}/beatgrid-fallback")
    assert r.status_code == 200, r.text
    assert r.json()["backend"] == "librosa+madmom"
    assert r.json()["beatgrid"]["beat_count"] > 0
