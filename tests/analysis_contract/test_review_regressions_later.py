"""Regressions for review rounds three through five on PR #1549.

[if] any round-three-to-five review finding on PR #1549 recurs [then] fail, [else stop].

Split from ``test_review_regressions.py`` at the round boundary: that file
crossed the 600-line-per-Python-file limit as the rounds accumulated. Same
discipline applies to every test here -- the claim was RUN and the wrong
behavior observed before anything was changed, and each fix goes red under a
mutation back to its pre-fix source.

-Claude
"""
from __future__ import annotations

import sqlite3

import pytest

from apps.analysis import store as analysis_store
from apps.analysis.lanes import LaneContractError, LaneResult
from apps.analysis.record import RecordContractError
from apps.shared.state import db as state_db
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


#-----------------------------------------------------------------------------
# P2 round 5: pitch class is a discrete identity, not a rounded number
#-----------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [-0.5, 11.9, 3.5])
def test_a_fractional_pitch_class_is_refused(db, bad: float) -> None:
    """Reproduced: int(-0.5) is 0 and int(11.9) is 11, both silently in range."""
    payload = key_payload()
    payload["pitch_class"] = bad
    with pytest.raises(LaneContractError, match="integer"):
        analysis_store.upsert_record(
            own_record(lane="key", result=LaneResult(status="ok", payload=payload)),
            conn=db,
        )


@pytest.mark.parametrize("out_of_range", [-1, 12])
def test_an_out_of_range_integer_pitch_class_is_still_refused(
    db, out_of_range: int,
) -> None:
    """The control: the range check must survive the type check landing on top."""
    payload = key_payload()
    payload["pitch_class"] = out_of_range
    with pytest.raises(LaneContractError, match=r"0\.\.11"):
        analysis_store.upsert_record(
            own_record(lane="key", result=LaneResult(status="ok", payload=payload)),
            conn=db,
        )


def test_every_valid_pitch_class_is_accepted(db) -> None:
    """The other control: 0..11 inclusive, not 1..10."""
    from apps.analysis.lanes import validate_lane_payload

    for pitch_class in range(12):
        payload = key_payload()
        payload["pitch_class"] = pitch_class
        validate_lane_payload("key", payload)


#-----------------------------------------------------------------------------
# P2 round 6: a grid the deck cannot consume is not `ok`
#-----------------------------------------------------------------------------

def _beats(*specs) -> list[dict]:
    return [{"t": t, "n": n, "bpm": bpm} for t, n, bpm in specs]


@pytest.mark.parametrize(
    "beats,why",
    [
        (_beats((0.0, 1, 128.0)), "one beat"),
        (_beats((0.0, 1, 128.0), (0.5, 1.5, 128.0)), "fractional n"),
        (_beats((0.0, 1, 128.0), (0.5, 9, 128.0)), "n out of 1..4"),
        (_beats((0.0, 1, 128.0), (0.5, 2, 0.0)), "nonpositive bpm"),
        (_beats((-1.0, 1, 128.0), (0.5, 2, 128.0)), "negative t"),
        (_beats((0.0, 1, 128.0), (0.0, 2, 128.0)), "t not increasing"),
        (_beats((0.0, 1, 128.0), (0.5, 3, 128.0)), "broken 1-2-3-4 cadence"),
    ],
)
def test_a_grid_the_deck_would_reject_is_refused(db, beats, why: str) -> None:
    """Transcribed from `validateBeatGrid` in beat-sync-math.ts, the real consumer.

    The reviewer's own example, `{t: 0, n: 1.5, bpm: 0}`, passed before this.
    """
    from tests.analysis_contract.conftest import beatgrid_payload

    payload = beatgrid_payload()
    payload["beats"] = beats
    with pytest.raises(LaneContractError):
        analysis_store.upsert_record(
            own_record(result=LaneResult(status="ok", payload=payload)), conn=db
        )


def test_a_grid_the_deck_accepts_is_still_stored(db) -> None:
    """The control: the invariant must reject bad grids, not all grids.

    The conftest fixture is a real 1-2-3-4 cadence at 128 BPM, which is
    exactly what `validateBeatGrid` is written to accept.
    """
    assert analysis_store.upsert_record(own_record(), conn=db).inserted is True
    assert db.execute(
        "SELECT value FROM analysis_projection WHERE field='bpm'"
    ).fetchone()[0] == 128.0


#-----------------------------------------------------------------------------
# P2 round 7: a version the ranking cannot tell apart defeats order independence
#-----------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["1.0.0-01", "1.0.0-alpha..1", "1.0.0-", "1.0.0-1."])
def test_a_malformed_semver_prerelease_is_refused(bad: str) -> None:
    """`1.0.0-01` ranked IDENTICALLY to `1.0.0-1`, so two distinct versions

    could not be ordered and the write-order independence this store promises
    became order-DEPENDENT for those rows.
    """
    from apps.analysis.lanes import SemverError, semver_key

    with pytest.raises(SemverError):
        semver_key(bad)


def test_valid_prereleases_still_rank_correctly() -> None:
    """The control: strictness must reject malformed input, not semver itself."""
    from apps.analysis.lanes import semver_key

    assert semver_key("1.0.0") > semver_key("1.0.0-rc1")
    assert semver_key("1.0.0-alpha.2") > semver_key("1.0.0-alpha.1")
    # Semver 2.0.0 rule 11: a NUMERIC identifier has LOWER precedence than an
    # alphanumeric one. I asserted this backwards first and the test caught it.
    assert semver_key("1.0.0-1") < semver_key("1.0.0-alpha")
    for good in ("1.0.0-1", "1.0.0-alpha.1", "1.0.0-rc1", "1.0.0-0", "1.0.0"):
        semver_key(good)


def test_the_store_refuses_a_record_whose_version_cannot_be_ranked(db) -> None:
    """The hole was only dangerous because such rows could become canonical."""
    with pytest.raises(RecordContractError, match="semver"):
        analysis_store.upsert_record(own_record(version="1.0.0-01"), conn=db)
    assert db.execute("SELECT count(*) FROM analysis").fetchone()[0] == 0


#-----------------------------------------------------------------------------
# P1 round 7: a GET must not migrate the database
#-----------------------------------------------------------------------------

def test_the_source_get_does_not_create_or_migrate_anything(tmp_path) -> None:
    """Reproduced by shape: `_open` used `store.open_conn`, which is READ-WRITE.

    A nominal read was applying the shared-state migrations and the analysis
    DDL, from a host that may not hold the writer lock and during a lock-probe
    outage -- both states `deps.py` exists to exclude.
    """
    from fastapi.testclient import TestClient

    from apps.shared.state import db as state_db
    from apps.webui.server.app import create_app

    path = tmp_path / "state.db"
    state_db.open_rw(path).close()
    probe = sqlite3.connect(path)
    before = sorted(
        r[0] for r in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")
    )
    assert "analysis_projection" not in before, "precondition: not yet provisioned"
    probe.close()

    app = create_app()
    app.state.analysis_db_path = path
    with TestClient(app) as client:
        body = client.get("/api/v1/analysis/source")
    assert body.status_code == 200
    assert body.json()["lanes"]["key"]["effective"] == "rbx"

    probe = sqlite3.connect(path)
    after = sorted(
        r[0] for r in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")
    )
    probe.close()
    assert after == before, f"the GET created {sorted(set(after) - set(before))}"


def test_the_source_get_works_on_a_read_only_database(tmp_path) -> None:
    """The control: read-only must still ANSWER, not just refrain from writing."""
    import os

    from fastapi.testclient import TestClient

    from apps.analysis import store as analysis_store
    from apps.webui.server.app import create_app

    path = tmp_path / "state.db"
    conn = analysis_store.open_conn(path)
    conn.execute(
        "INSERT INTO analysis_source_default VALUES ('loudness', 'own', ?)", (STAMP,)
    )
    conn.commit()
    conn.close()
    os.chmod(path, 0o444)
    try:
        app = create_app()
        app.state.analysis_db_path = path
        with TestClient(app) as client:
            body = client.get("/api/v1/analysis/source").json()
        assert body["lanes"]["loudness"]["effective"] == "own"
    finally:
        os.chmod(path, 0o644)
