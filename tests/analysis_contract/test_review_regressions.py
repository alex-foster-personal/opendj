"""Regressions for the five review findings on PR #1549, each reproduced first.

[if] any of the five review findings on PR #1549 recurs [then] fail, [else stop].

Every test below was written against a REPRODUCTION, not against the review
text: the claim was run, the wrong behavior observed, and only then fixed.
Each therefore goes red against the pre-fix source.

-Claude
"""
from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from apps.analysis import selection
from apps.analysis import store as analysis_store
from apps.analysis.canonical import canonical_pointer
from apps.analysis.lanes import LaneResult
from apps.analysis.record import AnalysisRecord
from apps.shared.state import db as state_db
from apps.smartlists.evaluator import evaluate
from apps.webui.server.app import create_app
from apps.webui.server.routes.analysis import _load_latest_record
from tests.analysis_contract.conftest import key_payload, own_record

pytestmark = pytest.mark.requirement("NATIVE-04")

STAMP = "2026-01-01T00:00:00Z"


def _bare_db(tmp_path):
    """A state.db with the Phase 5 schema and NO analysis tables at all."""
    path = tmp_path / "state.db"
    state_db.open_rw(path).close()
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, created_at, updated_at) "
        "VALUES ('t1', 'inferred', 'x', ?, ?)", (STAMP, STAMP),
    )
    conn.execute(
        "INSERT INTO track_fields (stable_id, field_name, value_json, source, "
        "confidence, modified_at) VALUES ('t1', 'key', '\"5A\"', 'rekordbox', 1.0, ?)",
        (STAMP,),
    )
    conn.commit()
    return path, conn


#-----------------------------------------------------------------------------
# P1: a promoted lane on a database with no projection store
#-----------------------------------------------------------------------------

def test_smartlist_on_a_promoted_lane_matches_nothing_rather_than_raising(
    tmp_path,
) -> None:
    """Reproduced: EvaluatorError 'no such table: analysis_projection'."""
    _, conn = _bare_db(tmp_path)
    selection.set_default(conn, "key", "own")
    conn.commit()
    assert conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE name='analysis_projection'"
    ).fetchone()[0] == 0, "precondition: the projection store must be absent"

    # Matches NOTHING: there are no own rows. Not the rekordbox 5A, and not
    # an exception that takes the whole smartlist down.
    assert evaluate({"field": "key", "op": "=", "value": "8A"}, conn) == []
    assert evaluate({"field": "key", "op": "=", "value": "5A"}, conn) == []


def test_promoting_a_lane_over_http_creates_the_store_that_word_refers_to(
    tmp_path,
) -> None:
    path, conn = _bare_db(tmp_path)
    conn.close()
    app = create_app()
    app.state.analysis_db_path = path
    with TestClient(app) as client:
        body = client.put(
            "/api/v1/analysis/source", json={"lane": "key", "default": "own"}
        )
    assert body.status_code == 200, body.text
    assert body.json()["lanes"]["key"]["effective"] == "own"
    check = sqlite3.connect(path)
    try:
        present = {
            r[0] for r in check.execute(
                "SELECT name FROM sqlite_master WHERE name IN "
                "('analysis', 'analysis_canonical', 'analysis_projection')"
            )
        }
        assert present == {"analysis", "analysis_canonical", "analysis_projection"}
    finally:
        check.close()


#-----------------------------------------------------------------------------
# P1: a failed key-segment analysis must not read as "never ran"
#-----------------------------------------------------------------------------

def test_a_failed_segments_block_keeps_its_own_status_and_reason(db) -> None:
    payload = key_payload()
    payload["segments"] = {
        "status": "failed", "reason": "no_downbeats_for_bar_sync", "segments": [],
    }
    analysis_store.upsert_record(
        own_record(lane="key", result=LaneResult(status="ok", payload=payload)),
        conn=db,
    )
    row = db.execute(
        "SELECT value, status, reason FROM analysis_projection "
        "WHERE stable_id='t1' AND field='key_change_count'"
    ).fetchone()
    assert row == (None, "failed", "no_downbeats_for_bar_sync")
    # And the global key, which DID succeed, is untouched by that failure.
    assert db.execute(
        "SELECT value, status FROM analysis_projection "
        "WHERE stable_id='t1' AND field='key'"
    ).fetchone() == ("8A", "ok")


def test_a_missing_segments_block_is_still_distinguishable_from_a_failed_one(
    db,
) -> None:
    """The control: the two non-ok states must not collapse into each other."""
    payload = key_payload()
    payload["segments"] = {"status": "missing", "reason": None, "segments": []}
    analysis_store.upsert_record(
        own_record(lane="key", result=LaneResult(status="ok", payload=payload)),
        conn=db,
    )
    assert db.execute(
        "SELECT status FROM analysis_projection "
        "WHERE stable_id='t1' AND field='key_change_count'"
    ).fetchone()[0] == "missing"


#-----------------------------------------------------------------------------
# P2: an unchanged re-run must not move the track's ETag
#-----------------------------------------------------------------------------

def test_an_unchanged_upsert_leaves_pointer_and_projection_timestamps_alone(
    db,
) -> None:
    record = own_record()
    analysis_store.upsert_record(record, conn=db)
    before = db.execute(
        "SELECT updated_at FROM analysis_projection "
        "WHERE stable_id='t1' AND field='bpm'"
    ).fetchone()[0]
    ptr_before = db.execute(
        "SELECT updated_at FROM analysis_canonical WHERE stable_id='t1'"
    ).fetchone()[0]

    time.sleep(0.01)
    result = analysis_store.upsert_record(record, conn=db)
    assert result.unchanged is True

    assert db.execute(
        "SELECT updated_at FROM analysis_projection "
        "WHERE stable_id='t1' AND field='bpm'"
    ).fetchone()[0] == before
    assert db.execute(
        "SELECT updated_at FROM analysis_canonical WHERE stable_id='t1'"
    ).fetchone()[0] == ptr_before


def test_a_changed_record_still_moves_the_timestamp(db) -> None:
    """The control: freezing the stamp must be conditional, not unconditional."""
    from tests.analysis_contract.conftest import beatgrid_payload

    analysis_store.upsert_record(own_record(), conn=db)
    before = db.execute(
        "SELECT updated_at FROM analysis_projection "
        "WHERE stable_id='t1' AND field='bpm'"
    ).fetchone()[0]
    time.sleep(0.01)
    analysis_store.upsert_record(
        own_record(
            version="1.1.0",
            result=LaneResult(status="ok", payload=beatgrid_payload(bpm=130.0)),
        ),
        conn=db,
    )
    after = db.execute(
        "SELECT value, updated_at FROM analysis_projection "
        "WHERE stable_id='t1' AND field='bpm'"
    ).fetchone()
    assert after[0] == 130.0
    assert after[1] != before
    assert canonical_pointer(db, "t1", "beatgrid") == ("own_beatgrid.inapp", "1.1.0")


#-----------------------------------------------------------------------------
# P2: own rows must not shadow the legacy latest-row readers
#-----------------------------------------------------------------------------

def _legacy_with_downbeats(stable_id: str, when: datetime) -> AnalysisRecord:
    return AnalysisRecord(
        stable_id=stable_id, backend="librosa-only",
        backend_version="librosa==0.10.2", analyzed_at=when,
        duration_s=200.0, sample_rate=44100, bpm=128.0, bpm_confidence=0.9,
        key_camelot="8A", key_openkey="1m", key_confidence=0.8, energy=5,
        downbeats_s=[0.0, 1.875, 3.75, 5.625],
    )


def test_a_later_own_row_does_not_shadow_the_legacy_beatgrid_row(tmp_path) -> None:
    """Reproduced: /beatgrid-fallback would have served an own_key row's empty grid."""
    path = tmp_path / "state.db"
    conn = analysis_store.open_conn(path)
    t0 = datetime(2026, 9, 1, tzinfo=UTC)
    analysis_store.upsert_record(_legacy_with_downbeats("t1", t0), conn=conn)
    first = _load_latest_record(path, "t1", None)
    assert first is not None and first.downbeats_s == [0.0, 1.875, 3.75, 5.625]

    later = own_record(lane="key", result=LaneResult(
        status="failed", reason="no_tonal_center"))
    import dataclasses

    analysis_store.upsert_record(
        dataclasses.replace(later, analyzed_at=t0 + timedelta(hours=1)), conn=conn
    )
    latest = _load_latest_record(path, "t1", None)
    assert latest is not None
    assert latest.backend == "librosa-only"
    assert latest.downbeats_s == [0.0, 1.875, 3.75, 5.625]
    conn.close()


def test_naming_an_own_backend_explicitly_still_returns_it(tmp_path) -> None:
    """The control: the exclusion is for the UNFILTERED query only."""
    path = tmp_path / "state.db"
    conn = analysis_store.open_conn(path)
    analysis_store.upsert_record(own_record(), conn=conn)
    got = _load_latest_record(path, "t1", "own_beatgrid.inapp")
    assert got is not None and got.backend == "own_beatgrid.inapp"
    conn.close()


#-----------------------------------------------------------------------------
# P2: a PUT is all or nothing
#-----------------------------------------------------------------------------

def test_a_put_with_a_valid_default_and_an_invalid_toggle_persists_nothing(
    tmp_path,
) -> None:
    path, conn = _bare_db(tmp_path)
    conn.close()
    app = create_app()
    app.state.analysis_db_path = path
    with TestClient(app) as client:
        resp = client.put("/api/v1/analysis/source", json={
            "lane": "key", "default": "own", "toggle": "sometimes",
        })
        assert resp.status_code == 422
        after = client.get("/api/v1/analysis/source").json()
    assert after["lanes"]["key"]["default"] == "rbx"
    assert after["lanes"]["key"]["effective"] == "rbx"


def test_a_put_with_both_halves_valid_still_applies_both(tmp_path) -> None:
    """The control: the guard must reject invalid input, not all input."""
    path, conn = _bare_db(tmp_path)
    conn.close()
    app = create_app()
    app.state.analysis_db_path = path
    try:
        with TestClient(app) as client:
            body = client.put("/api/v1/analysis/source", json={
                "lane": "waveform", "default": "own", "toggle": "rbx",
            }).json()
        assert body["lanes"]["waveform"] == {
            "default": "own", "toggle": "rbx", "effective": "rbx",
        }
    finally:
        selection.reset_toggles()
