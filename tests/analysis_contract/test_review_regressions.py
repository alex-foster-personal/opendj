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
from apps.analysis.lanes import LaneContractError, LaneResult
from apps.analysis.record import AnalysisRecord, RecordContractError
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
    """Reproduced: EvaluatorError 'no such table: analysis_projection'.

    The promotion is written with raw SQL rather than through
    `selection.set_default`, because that function now provisions the whole
    analysis schema (the writer half of this same finding) and so can no
    longer reach the state. The state is still reachable in the wild: a
    database promoted by an older build, or edited directly. The READER
    hardening is what this pins, and it has to be pinned separately from the
    writer or fixing one would silently retire the test for the other.
    """
    _, conn = _bare_db(tmp_path)
    conn.execute(
        "CREATE TABLE analysis_source_default (lane TEXT PRIMARY KEY, "
        "source TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO analysis_source_default VALUES ('key', 'own', ?)", (STAMP,)
    )
    conn.commit()
    assert conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE name='analysis_projection'"
    ).fetchone()[0] == 0, "precondition: the projection store must be absent"
    assert selection.effective_source(conn, "key") == "own", (
        "precondition: the lane really is promoted"
    )

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


#-----------------------------------------------------------------------------
# P1 round 2: the record, the pointer and the projection are one unit
#-----------------------------------------------------------------------------

def test_a_failing_refresh_leaves_no_half_written_state(db, monkeypatch) -> None:
    """Reproduced: the connection is autocommit, so each statement was durable.

    The seam is a REAL failure inside the rebuild, injected at the SQL layer
    (a dropped table), not a patched function: the point is that whatever
    goes wrong mid-refresh, nothing durable survives it.
    """
    analysis_store.upsert_record(own_record(version="1.0.0"), conn=db)
    before_rows = db.execute("SELECT count(*) FROM analysis").fetchone()[0]
    before_proj = db.execute(
        "SELECT field, value, backend_version FROM analysis_projection ORDER BY field"
    ).fetchall()
    assert before_rows == 1 and before_proj, "precondition: there is state to protect"

    db.execute("ALTER TABLE analysis_projection RENAME TO analysis_projection_hidden")
    with pytest.raises(sqlite3.OperationalError):
        analysis_store.upsert_record(own_record(version="2.0.0"), conn=db)
    db.execute("ALTER TABLE analysis_projection_hidden RENAME TO analysis_projection")

    # The 2.0.0 record must NOT be durable, and neither must a pointer to it.
    assert db.execute("SELECT count(*) FROM analysis").fetchone()[0] == before_rows
    assert canonical_pointer(db, "t1", "beatgrid") == ("own_beatgrid.inapp", "1.0.0")
    assert db.execute(
        "SELECT field, value, backend_version FROM analysis_projection ORDER BY field"
    ).fetchall() == before_proj


def test_a_successful_upsert_still_commits(db) -> None:
    """The control: the transaction must commit, not merely not-rollback."""
    analysis_store.upsert_record(own_record(version="1.0.0"), conn=db)
    fresh = sqlite3.connect(db.execute("PRAGMA database_list").fetchone()[2])
    try:
        assert fresh.execute("SELECT count(*) FROM analysis").fetchone()[0] == 1
        assert fresh.execute(
            "SELECT count(*) FROM analysis_projection"
        ).fetchone()[0] == 2
    finally:
        fresh.close()


#-----------------------------------------------------------------------------
# P1 round 2: a measurement that is not a number is not a measurement
#-----------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_lane_number_is_refused(db, bad: float) -> None:
    """Reproduced: NaN stored as `status: ok` with a NULL value."""
    from tests.analysis_contract.conftest import beatgrid_payload

    payload = beatgrid_payload()
    payload["bpm"] = bad
    with pytest.raises(LaneContractError, match="finite"):
        analysis_store.upsert_record(
            own_record(result=LaneResult(status="ok", payload=payload)), conn=db
        )
    assert db.execute("SELECT count(*) FROM analysis_projection").fetchone()[0] == 0


def test_a_non_finite_number_inside_a_beat_is_refused_too(db) -> None:
    """The class, not the instance: every numeric field goes through one check."""
    from tests.analysis_contract.conftest import beatgrid_payload

    payload = beatgrid_payload()
    payload["beats"][2]["t"] = float("nan")
    with pytest.raises(LaneContractError, match="finite"):
        analysis_store.upsert_record(
            own_record(result=LaneResult(status="ok", payload=payload)), conn=db
        )


def test_finite_measurements_are_still_accepted(db) -> None:
    """The control: the guard must reject non-numbers, not numbers."""
    result = analysis_store.upsert_record(own_record(), conn=db)
    assert result.inserted is True
    assert db.execute(
        "SELECT value FROM analysis_projection WHERE field='bpm'"
    ).fetchone()[0] == 128.0


#-----------------------------------------------------------------------------
# P1 round 3: an EXISTING database must gain the new tables
#-----------------------------------------------------------------------------

def test_an_already_stamped_database_gains_the_new_tables(tmp_path) -> None:
    """Reproduced: apply_migrations returns at `current >= SCHEMA_VERSION`.

    Adding DDL to the v1 rung alone was invisible to every fresh-database
    test AND to every existing install, which is the worst combination: the
    tests stay green and no user ever gets the table.
    """
    from apps.engine_core.store import schema as consolidated

    path = tmp_path / "engine.db"
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        assert consolidated.apply_migrations(conn) == consolidated.SCHEMA_VERSION
        names = ("analysis_canonical", "analysis_projection", "analysis_source_default")
        present = lambda: {  # noqa: E731
            n: conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE name=?", (n,)
            ).fetchone()[0]
            for n in names
        }
        assert all(present().values()), "fresh database must create all three"

        # Wind the file back to what a pre-v2 install looks like.
        for n in names:
            conn.execute(f"DROP TABLE {n}")
        conn.execute("DELETE FROM schema_meta")
        conn.execute(
            "INSERT INTO schema_meta (version, applied_at) VALUES (?, ?)",
            (consolidated.VERSION_OFFSET + 1, "2026-01-01T00:00:00Z"),
        )
        assert not any(present().values()), "precondition: the tables are gone"

        consolidated.apply_migrations(conn)
        assert all(present().values()), (
            "an existing install must gain the tables on upgrade"
        )
    finally:
        conn.close()


#-----------------------------------------------------------------------------
# P2 round 3: a digest field must contain a digest
#-----------------------------------------------------------------------------

def test_a_placeholder_decode_fingerprint_is_refused(db) -> None:
    """`sha256:decode-fixture` was accepted and means nothing."""
    import dataclasses

    bad = dataclasses.replace(own_record(), decode_fingerprint="sha256:decode-fixture")
    with pytest.raises(RecordContractError, match="not a sha256 digest"):
        analysis_store.upsert_record(bad, conn=db)
    assert db.execute("SELECT count(*) FROM analysis").fetchone()[0] == 0


def test_a_real_digest_is_still_accepted(db) -> None:
    """The control: the guard must reject malformed digests, not all of them."""
    assert analysis_store.upsert_record(own_record(), conn=db).inserted is True


#-----------------------------------------------------------------------------
# P2 round 3: every waveform band sample, not just the count
#-----------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "loud"])
def test_a_bad_waveform_band_sample_is_refused(db, bad: object) -> None:
    from tests.analysis_contract.conftest import waveform_payload

    payload = waveform_payload()
    payload["preview"]["mid"][1] = bad
    with pytest.raises(LaneContractError):
        analysis_store.upsert_record(
            own_record(lane="waveform", result=LaneResult(status="ok", payload=payload)),
            conn=db,
        )


def test_a_well_formed_waveform_is_still_accepted(db) -> None:
    assert analysis_store.upsert_record(
        own_record(lane="waveform"), conn=db
    ).inserted is True


#-----------------------------------------------------------------------------
# P2 round 4: the lane-level confidence is a measurement too
#-----------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["very", float("nan"), float("inf")])
def test_a_lane_confidence_that_is_not_a_finite_number_is_refused(
    db, bad: object,
) -> None:
    """It is copied verbatim into ProvenanceOut.confidence: float | None."""
    from tests.analysis_contract.conftest import loudness_payload

    with pytest.raises(LaneContractError, match="confidence"):
        analysis_store.upsert_record(
            own_record(
                lane="loudness",
                result=LaneResult(
                    status="ok", confidence=bad, payload=loudness_payload()
                ),
            ),
            conn=db,
        )


def test_a_real_confidence_and_none_both_still_pass(db) -> None:
    """The control: `None` is a legitimate value, not a rejected one."""
    from tests.analysis_contract.conftest import loudness_payload

    for confidence in (0.91, None):
        analysis_store.upsert_record(
            own_record(
                lane="loudness",
                result=LaneResult(
                    status="ok", confidence=confidence, payload=loudness_payload()
                ),
            ),
            conn=db,
        )
    assert db.execute(
        "SELECT count(*) FROM analysis_projection WHERE field='loudness_lufs'"
    ).fetchone()[0] == 1


#-----------------------------------------------------------------------------
# P2 round 4: a failed key-segment block must name its reason
#-----------------------------------------------------------------------------

def test_failed_key_segments_without_a_reason_are_refused(db) -> None:
    """The projection surfaces that reason; it cannot invent one."""
    payload = key_payload()
    payload["segments"] = {"status": "failed", "reason": None, "segments": []}
    with pytest.raises(LaneContractError, match="without a reason"):
        analysis_store.upsert_record(
            own_record(lane="key", result=LaneResult(status="ok", payload=payload)),
            conn=db,
        )


def test_failed_key_segments_with_a_reason_still_project_it(db) -> None:
    """The control, and the round-three fix still holding."""
    payload = key_payload()
    payload["segments"] = {
        "status": "failed", "reason": "no_downbeats_for_bar_sync", "segments": [],
    }
    analysis_store.upsert_record(
        own_record(lane="key", result=LaneResult(status="ok", payload=payload)),
        conn=db,
    )
    assert db.execute(
        "SELECT status, reason FROM analysis_projection "
        "WHERE field='key_change_count'"
    ).fetchone() == ("failed", "no_downbeats_for_bar_sync")


#-----------------------------------------------------------------------------
# P2 round 4: the migration audit must see every rung it creates
#-----------------------------------------------------------------------------

def test_the_audit_reference_covers_the_v2_tables(tmp_path) -> None:
    """A malformed v2 table must be REFUSED, not preserved by IF NOT EXISTS.

    Reproduced by shape rather than by claim: the reference the shape audit
    compares against was built from the v1 rung only, so nothing in v2 was in
    it and a wrong-shaped table sailed through to a v2 stamp.
    """
    from apps.engine_core.store import schema as consolidated

    reference = consolidated._reference_objects()
    for name in consolidated.TABLES["native_analysis_v1"]:
        assert name in reference, (
            f"{name} is created by the ladder but absent from the audit "
            "reference, so its shape is never checked"
        )

    path = tmp_path / "engine.db"
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        # A v1-era database carrying a WRONG-SHAPED v2 table.
        conn.execute(
            "CREATE TABLE analysis_canonical (stable_id TEXT PRIMARY KEY)"
        )
        with pytest.raises(Exception) as excinfo:
            consolidated.apply_migrations(conn)
        assert "analysis_canonical" in str(excinfo.value)
    finally:
        conn.close()
