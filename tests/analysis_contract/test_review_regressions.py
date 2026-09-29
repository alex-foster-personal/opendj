"""Regressions for the five review findings on PR #1549, each reproduced first.

[if] any of the five review findings on PR #1549 recurs [then] fail, [else stop].

Every test below was written against a REPRODUCTION, not against the review
text: the claim was run, the wrong behavior observed, and only then fixed.
Each therefore goes red against the pre-fix source.

-Claude
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from apps.analysis import selection
from apps.analysis import store as analysis_store
from apps.analysis.canonical import canonical_pointer
from apps.analysis.lanes import LaneContractError, LaneResult
from apps.analysis.record import AnalysisRecord
from apps.shared.state import db as state_db
from apps.smartlists.evaluator import evaluate
from apps.webui.server.routes.analysis import _load_latest_record
from tests.analysis_contract.conftest import key_payload, own_record

pytestmark = pytest.mark.requirement("NATIVE-04")

STAMP = "2026-01-01T00:00:00Z"


def _app_on(path):
    """An app whose SERVING backend is the same database the endpoint writes.

    `create_app()` defaults to InMemoryBackend, and the endpoint now refuses
    to persist a lane default on one -- a promotion the running backend
    cannot read would report `own` while /tracks kept serving rekordbox
    (Codex P1). These tests want the real pairing, so they wire it.
    """
    from apps.webui.server.app import create_app
    from apps.webui.server.sqlite_backend import SqliteBackend

    app = create_app()
    app.state.analysis_db_path = path
    app.state.backend = SqliteBackend(path)
    return app


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
    app = _app_on(path)
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
    app = _app_on(path)
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
    app = _app_on(path)
    try:
        with TestClient(app) as client:
            body = client.put("/api/v1/analysis/source", json={
                "lane": "key", "default": "own", "toggle": "rbx",
            }).json()
        assert body["lanes"]["key"] == {
            "default": "own", "toggle": "rbx", "toggle_revision": 1, "effective": "rbx",
        }
    finally:
        selection.reset_toggles()


#-----------------------------------------------------------------------------
# P1/P2: toggle ABA and a combined write's partial-success failure mode
#-----------------------------------------------------------------------------

def test_expected_toggle_revision_over_http_closes_the_aba_gap(tmp_path) -> None:
    """discussion_r3974993963 P1 BLOCKING, HTTP half, reproduced first: a
    rollback that captured the revision its own switch-set PUT landed at
    must be refused once the toggle round-trips own -> rbx -> own before the
    rollback runs, even though the CURRENT value equals what it still names
    in `expected_toggle`.
    """
    path, conn = _bare_db(tmp_path)
    conn.close()
    app = _app_on(path)
    try:
        with TestClient(app) as client:
            first = client.put(
                "/api/v1/analysis/source", json={"lane": "key", "toggle": "own"}
            ).json()
            revision = first["lanes"]["key"]["toggle_revision"]
            # An external agent's own PUTs, unrelated to the rollback below.
            client.put("/api/v1/analysis/source", json={"lane": "key", "toggle": "rbx"})
            client.put("/api/v1/analysis/source", json={"lane": "key", "toggle": "own"})

            stale = client.put(
                "/api/v1/analysis/source",
                json={
                    "lane": "key",
                    "toggle": "unset",
                    "expected_toggle": "own",
                    "expected_toggle_revision": revision,
                },
            )
            assert stale.status_code == 409
            after = client.get("/api/v1/analysis/source").json()
            assert after["lanes"]["key"]["toggle"] == "own", (
                "the ABA-stale rollback must not erase the external agent's newer decision"
            )

            # Mutate-both-directions control: the identical rollback, given
            # the CURRENT revision instead of the stale one, must succeed.
            current_revision = after["lanes"]["key"]["toggle_revision"]
            fresh = client.put(
                "/api/v1/analysis/source",
                json={
                    "lane": "key",
                    "toggle": "unset",
                    "expected_toggle": "own",
                    "expected_toggle_revision": current_revision,
                },
            )
            assert fresh.status_code == 200
            assert fresh.json()["lanes"]["key"]["toggle"] == "unset"
    finally:
        selection.reset_toggles()


def test_a_combined_write_that_fails_on_the_default_restores_the_toggle(
    tmp_path,
) -> None:
    """discussion_r3974993965 P2 BLOCKING, issue #3189: a combined PUT that
    cannot open the database writably must leave the toggle at its prior value
    and `toggle_revision` at launch state `0` because `_apply_toggle` is
    never reached.

    Since commit `3df9acb93` the route opens the writable connection before
    calling `_apply_toggle`, so a real `BEGIN EXCLUSIVE` lock held before any
    priming PUT blocks that open on migration work. The end state matches an
    uncompensated rollback (toggle `unset`, default `rbx`) but revision `0`
    correctly reports that no toggle write occurred. See
    `test_a_combined_write_compensation_bumps_toggle_revision` for the path
    where the toggle write lands and compensation bumps the counter to `2`.

    The failure is a REAL `sqlite3.OperationalError`: a second connection to
    the SAME file holds `BEGIN EXCLUSIVE` so the route's own write blocks for
    the real `busy_timeout` and then genuinely fails, no production code
    replaced (AGENTS.md's no-mocks contract).
    """
    path, conn = _bare_db(tmp_path)
    conn.close()
    app = _app_on(path)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            locker = sqlite3.connect(str(path), timeout=0.1)
            locker.execute("BEGIN EXCLUSIVE")
            try:
                resp = client.put(
                    "/api/v1/analysis/source",
                    json={"lane": "beatgrid", "toggle": "own", "default": "own"},
                )
            finally:
                locker.rollback()
                locker.close()
            assert resp.status_code == 503, resp.text[:300]  # busy is 503 since 0a0d095fb

            after = client.get("/api/v1/analysis/source").json()
            assert after["lanes"]["beatgrid"]["toggle"] == "unset", (
                "the toggle must still be at launch state because the open-time "
                "failure never reached `_apply_toggle`"
            )
            assert after["lanes"]["beatgrid"]["default"] == "rbx", (
                "the durable half must not have taken effect either"
            )
            # 3df9acb93: writable open precedes `_apply_toggle`, so an
            # open-time lock failure is not a toggle write and revision stays 0.
            assert after["lanes"]["beatgrid"]["toggle_revision"] == 0

            # Control: the SAME shape of PUT, with the lock released, applies
            # both halves normally - the fix must not refuse every combined
            # write, only fail closed when the durable path cannot open.
            ok = client.put(
                "/api/v1/analysis/source",
                json={"lane": "beatgrid", "toggle": "own", "default": "own"},
            )
            assert ok.status_code == 200
            assert ok.json()["lanes"]["beatgrid"] == {
                "default": "own", "toggle": "own", "toggle_revision": 1, "effective": "own",
            }
    finally:
        selection.reset_toggles()


def test_a_combined_write_compensation_bumps_toggle_revision(tmp_path) -> None:
    """issue #3189: when `_apply_toggle` has already run and the durable half
    fails, compensation is a second real `write_toggle` and clients must see
    revision 2 so they know to re-read after the rollback.

    Uses the production `_commit_default_or_compensate` helper with a genuine
    read-only SQLite connection so `set_default`/`commit` fail through SQLite,
    not through a mock or patched function.
    """
    from apps.webui.server.routes.analysis_source import _commit_default_or_compensate

    path, conn = _bare_db(tmp_path)
    conn.close()
    app = _app_on(path)
    try:
        with TestClient(app) as client:
            prime = client.put(
                "/api/v1/analysis/source", json={"lane": "beatgrid", "default": "rbx"}
            )
            assert prime.status_code == 200

            toggle_write = selection.write_toggle("beatgrid", "own")
            assert toggle_write == selection.ToggleWrite(
                previous="unset", current="own", revision=1
            )

            ro = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            ro.execute("PRAGMA query_only = ON")
            try:
                with pytest.raises(sqlite3.Error):
                    _commit_default_or_compensate(
                        ro, "beatgrid", "own", "own", toggle_write
                    )
            finally:
                ro.close()

            after = client.get("/api/v1/analysis/source").json()
            assert after["lanes"]["beatgrid"]["toggle"] == "unset"
            assert after["lanes"]["beatgrid"]["default"] == "rbx"
            assert after["lanes"]["beatgrid"]["toggle_revision"] == 2, (
                "one write for the switch, one for the compensation"
            )
    finally:
        selection.reset_toggles()


def test_a_combined_write_that_fails_never_clobbers_a_concurrent_agent_change(
    tmp_path,
) -> None:
    """Mutate-both-directions control for discussion_r3974993965: the
    compensation must ONLY restore what THIS request's own write displaced.
    An agent's unrelated write landing in the failure window must survive.

    Real concurrency, not a fabricated call sequence: a second connection
    holds `BEGIN EXCLUSIVE` so the route's write genuinely blocks for the
    real `busy_timeout`, and a background thread calls the process-local
    `selection.write_toggle` directly (a real concurrent agent never goes
    through this same HTTP request, so simulating one as a plain function
    call on another thread is the real shape of that failure, not a stand-in
    for the route's own SQL). `sqlite3` releases the GIL while blocked on a
    busy connection, so the thread's write reliably lands inside the window.
    """
    path, conn = _bare_db(tmp_path)
    conn.close()
    app = _app_on(path)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            prime = client.put(
                "/api/v1/analysis/source", json={"lane": "beatgrid", "default": "rbx"}
            )
            assert prime.status_code == 200

            locker = sqlite3.connect(str(path), timeout=0.1)
            locker.execute("BEGIN EXCLUSIVE")
            locker.execute(
                "INSERT INTO analysis_source_default (lane, source, updated_at) "
                "VALUES ('key', 'own', 'x')"
            )

            def _concurrent_agent() -> None:
                time.sleep(0.3)
                selection.write_toggle("beatgrid", "rbx")

            agent = threading.Thread(target=_concurrent_agent)
            agent.start()
            try:
                resp = client.put(
                    "/api/v1/analysis/source",
                    json={"lane": "beatgrid", "toggle": "own", "default": "own"},
                )
            finally:
                agent.join()
                locker.rollback()
                locker.close()
            assert resp.status_code == 503, resp.text[:300]  # busy is 503 since 0a0d095fb

            after = client.get("/api/v1/analysis/source").json()
            assert after["lanes"]["beatgrid"]["toggle"] == "rbx", (
                "a rollback that fires after a concurrent agent's own write must stand "
                "down, not clobber their newer value with this request's stale one"
            )
    finally:
        selection.reset_toggles()


def test_a_put_response_reports_its_own_write_not_a_concurrent_agents_later_one(
    tmp_path,
) -> None:
    """A PUT's `lanes[lane]` row must describe the write THIS request made.

    Reproduced first, against the webui unit suite: analysis-source.test.mjs
    "a failed switch never clobbers a concurrent agent-driven HTTP change
    during rollback" failed about 1 in 25 runs under load with "Missing
    expected rejection". A trace of a failing run showed the switch's own
    PUT answering `previous_toggle: rbx` (it displaced rbx and wrote own) but
    `toggle: unset, effective: rbx`, the AGENT's later write, re-read by a
    separate `source_state` call. The client adopted that as "no deck
    disagrees", skipped the refresh that should have failed, and resolved.

    Same real-concurrency shape as the test above: a second connection holds
    `BEGIN EXCLUSIVE` so the combined PUT's default commit blocks AFTER its
    toggle write, a thread lands an agent's toggle write inside that window,
    and the lock is released so the PUT then succeeds.
    """
    path, conn = _bare_db(tmp_path)
    conn.close()
    app = _app_on(path)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            prime = client.put(
                "/api/v1/analysis/source", json={"lane": "beatgrid", "default": "rbx"}
            )
            assert prime.status_code == 200

            # Released from the agent thread once its write has landed.
            locker = sqlite3.connect(str(path), timeout=0.1, check_same_thread=False)
            locker.execute("BEGIN EXCLUSIVE")
            agent_writes: list[selection.ToggleWrite | None] = []

            def _concurrent_agent_then_release() -> None:
                time.sleep(0.3)
                agent_writes.append(selection.write_toggle("beatgrid", "unset"))
                locker.rollback()
                locker.close()

            agent = threading.Thread(target=_concurrent_agent_then_release)
            agent.start()
            try:
                resp = client.put(
                    "/api/v1/analysis/source",
                    json={"lane": "beatgrid", "toggle": "own", "default": "own"},
                )
            finally:
                agent.join()
            assert resp.status_code == 200, resp.text[:300]

            # Precondition, not decoration: the agent's write must have
            # displaced THIS request's `own`, i.e. landed after its toggle
            # write and before its response. Anywhere else, this test proves
            # nothing and must say so rather than pass.
            (agent_write,) = agent_writes
            assert agent_write is not None and agent_write.previous == "own", agent_write

            assert resp.json()["previous_toggle"] == "unset"
            assert resp.json()["lanes"]["beatgrid"] == {
                "default": "own",
                "toggle": "own",
                "toggle_revision": agent_write.revision - 1,
                "effective": "own",
            }, "a PUT response must report its own write, not a concurrent agent's later one"

            # Control: the agent's write really is live, so the row above was
            # pinned to this request's write rather than read after the race.
            after = client.get("/api/v1/analysis/source").json()["lanes"]["beatgrid"]
            assert (after["toggle"], after["toggle_revision"]) == ("unset", agent_write.revision)
    finally:
        selection.reset_toggles()


def test_source_state_pins_only_the_written_lane_and_otherwise_reports_live(
    tmp_path,
) -> None:
    """`written` pins exactly one lane to the caller's own write; without it
    (a GET), every lane is the live state, value and revision as one pair."""
    _, conn = _bare_db(tmp_path)
    try:
        mine = selection.write_toggle("beatgrid", "own")
        selection.write_toggle("key", "own")
        agent = selection.write_toggle("beatgrid", "unset")
        assert mine is not None and agent is not None

        pinned = selection.source_state(conn, written=("beatgrid", mine))["lanes"]
        assert pinned["beatgrid"] == {
            "default": "rbx",
            "toggle": "own",
            "toggle_revision": mine.revision,
            "effective": "own",
        }
        assert pinned["key"]["toggle"] == "own", "pinning one lane must not touch another"

        live = selection.source_state(conn)["lanes"]["beatgrid"]
        assert live == {
            "default": "rbx",
            "toggle": "unset",
            "toggle_revision": agent.revision,
            "effective": "rbx",
        }, "a GET must keep reporting the live state, not any earlier write"
    finally:
        conn.close()
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
