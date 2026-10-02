"""The canonical pointer and the projection table, against a real state.db.

[if] write order changes the canonical pointer or the projection [then] fail, [else stop].

Spec: `specs/native-analysis-v1.md` section 3 ("Record"). Requirements:
NATIVE-09 (order independence), NATIVE-04 and NATIVE-07 (the read model).

Acceptance lines exercised here:
- [if] in-app is written before backfill, and backfill before in-app, for the
  same track and version [then] the canonical pointer is identical either way.
- [if] a bench candidate record is written [then] its `cand.*` backend is never
  eligible for the canonical pointer.
- [if] loudness lands in AnalysisRecord [then] loudness_lufs and loudness_dbtp
  are readable through analysis_projection.
- [if] own key is `failed` [then] the projection carries status failed and the
  reason, with a null value.

-Claude
"""
from __future__ import annotations

import sqlite3

import pytest

from apps.analysis import store as store_mod
from apps.analysis.canonical import canonical_pointer
from apps.analysis.lanes import LaneResult
from apps.analysis.record import RecordContractError
from tests.analysis_contract.conftest import key_payload, own_record

pytestmark = pytest.mark.requirement("NATIVE-09")


def _projection(conn: sqlite3.Connection, stable_id: str) -> dict[str, tuple]:
    rows = conn.execute(
        "SELECT field, value, status, reason, backend, backend_version "
        "FROM analysis_projection WHERE stable_id = ?",
        (stable_id,),
    ).fetchall()
    return {r[0]: tuple(r[1:]) for r in rows}


#-----------------------------------------------------------------------------
# order independence: the headline regression
#-----------------------------------------------------------------------------

def test_pointer_and_projection_do_not_depend_on_write_order(tmp_path) -> None:
    """Write backfill then inapp, and inapp then backfill. Compare both ends."""
    def build(order: list[str], path: str) -> tuple[tuple[str, str] | None, dict[str, tuple]]:
        conn = store_mod.open_conn(tmp_path / path)
        try:
            for producer in order:
                store_mod.upsert_record(
                    own_record(producer=producer, version="1.2.0"), conn=conn
                )
            return (canonical_pointer(conn, "t1", "beatgrid"), _projection(conn, "t1"))
        finally:
            conn.close()

    forward = build(["backfill", "inapp"], "a.db")
    reverse = build(["inapp", "backfill"], "b.db")
    assert forward[0] == reverse[0] == ("own_beatgrid.inapp", "1.2.0")
    assert forward[1] == reverse[1]
    assert forward[1]["bpm"][0] == 128.0


def test_higher_version_wins_over_producer_rank(db) -> None:
    store_mod.upsert_record(own_record(producer="inapp", version="1.0.0"), conn=db)
    store_mod.upsert_record(own_record(producer="backfill", version="1.1.0"), conn=db)
    assert canonical_pointer(db, "t1", "beatgrid") == ("own_beatgrid.backfill", "1.1.0")


def test_version_ordering_is_numeric_not_lexical(db) -> None:
    """0.10.0 must beat 0.9.0. A string sort would get this backwards."""
    store_mod.upsert_record(own_record(producer="inapp", version="0.9.0"), conn=db)
    store_mod.upsert_record(own_record(producer="backfill", version="0.10.0"), conn=db)
    assert canonical_pointer(db, "t1", "beatgrid") == ("own_beatgrid.backfill", "0.10.0")


# REQ: NATIVE-11
@pytest.mark.requirement("NATIVE-11")
def test_cand_rows_are_never_eligible(db) -> None:
    """[if] a row comes from a bench candidate [then] it never goes canonical, [else stop]."""
    store_mod.upsert_record(
        own_record(producer="cand", version="9.9.9",
                   backend="own_beatgrid.cand.beat_this"),
        conn=db,
    )
    assert canonical_pointer(db, "t1", "beatgrid") is None
    assert _projection(db, "t1") == {}

    store_mod.upsert_record(own_record(producer="backfill", version="0.1.0"), conn=db)
    assert canonical_pointer(db, "t1", "beatgrid") == ("own_beatgrid.backfill", "0.1.0")


def test_pre_v1_rows_are_never_eligible(db) -> None:
    """Negative control: the 347 stored librosa rows must not become canonical."""
    import dataclasses

    legacy = dataclasses.replace(
        own_record(), backend="librosa-only",
        backend_version="librosa==0.10.2.post1", producer="backfill",
        producer_version="", decode_fingerprint="", lanes={},
    )
    store_mod.upsert_record(legacy, conn=db)
    assert canonical_pointer(db, "t1", "beatgrid") is None


#-----------------------------------------------------------------------------
# projection contents
#-----------------------------------------------------------------------------

def test_beatgrid_projects_bpm_and_tempo_change_count(db) -> None:
    from tests.analysis_contract.conftest import beatgrid_payload

    record = own_record(
        result=LaneResult(status="ok", payload=beatgrid_payload(tempo_changes=2)),
    )
    store_mod.upsert_record(record, conn=db)
    proj = _projection(db, "t1")
    assert proj["bpm"][0] == 128.0
    assert proj["bpm"][1] == "ok"
    assert proj["tempo_change_count"][0] == 2


def test_key_projects_camelot_and_change_count(db) -> None:
    record = own_record(
        lane="key", result=LaneResult(status="ok", payload=key_payload(segments=3)),
    )
    store_mod.upsert_record(record, conn=db)
    proj = _projection(db, "t1")
    assert proj["key"][0] == "8A"
    # Three segments is TWO changes: a stable key is one segment.
    assert proj["key_change_count"][0] == 2


def test_loudness_projects_lufs_and_dbtp(db) -> None:
    store_mod.upsert_record(own_record(lane="loudness"), conn=db)
    proj = _projection(db, "t1")
    assert proj["loudness_lufs"][0] == -8.2
    assert proj["loudness_dbtp"][0] == -0.3


def test_a_failed_lane_projects_a_null_value_with_status_and_reason(db) -> None:
    record = own_record(
        lane="key",
        result=LaneResult(status="failed", reason="no_tonal_center"),
    )
    store_mod.upsert_record(record, conn=db)
    proj = _projection(db, "t1")
    assert proj["key"] == (None, "failed", "no_tonal_center",
                           "own_key.inapp", "1.0.0")


def test_key_segments_can_be_missing_while_the_key_itself_is_ok(db) -> None:
    """The segments block carries its own status; key_change_count follows it."""
    payload = key_payload()
    payload["segments"] = {
        "status": "missing", "reason": "no own downbeats yet", "segments": [],
    }
    store_mod.upsert_record(
        own_record(lane="key", result=LaneResult(status="ok", payload=payload)),
        conn=db,
    )
    proj = _projection(db, "t1")
    assert proj["key"][1] == "ok"
    assert proj["key_change_count"][0] is None
    assert proj["key_change_count"][1] == "missing"


def test_numeric_projection_values_compare_numerically_in_sql(db) -> None:
    """The typeless `value` column is load-bearing for smartlist operators."""
    from tests.analysis_contract.conftest import beatgrid_payload

    for sid, bpm in (("slow", 99.0), ("fast", 100.0)):
        store_mod.upsert_record(
            own_record(
                stable_id=sid,
                result=LaneResult(status="ok", payload=beatgrid_payload(bpm=bpm)),
            ),
            conn=db,
        )
    hits = db.execute(
        "SELECT stable_id FROM analysis_projection WHERE field='bpm' AND value > 99.5"
    ).fetchall()
    # Lexically '100.0' < '99.0', so a TEXT column would return the wrong row.
    assert [h[0] for h in hits] == ["fast"]


#-----------------------------------------------------------------------------
# the write boundary
#-----------------------------------------------------------------------------

def test_upsert_refuses_an_own_record_with_no_producer_version(db) -> None:
    import dataclasses

    bad = dataclasses.replace(own_record(), producer_version="", backend_version="")
    with pytest.raises(RecordContractError):
        store_mod.upsert_record(bad, conn=db)
    assert db.execute("SELECT count(*) FROM analysis").fetchone()[0] == 0


def test_upsert_of_a_legacy_record_still_works(db) -> None:
    """Positive control: the guard above must not have closed the legacy path."""
    import dataclasses

    legacy = dataclasses.replace(
        own_record(), backend="librosa-only",
        backend_version="librosa==0.10.2.post1", producer="backfill",
        producer_version="", decode_fingerprint="", lanes={},
    )
    result = store_mod.upsert_record(legacy, conn=db)
    assert result.inserted is True
    assert db.execute("SELECT count(*) FROM analysis").fetchone()[0] == 1


def test_no_own_value_ever_reaches_track_fields_or_its_history(db) -> None:
    """Spec section 3: read-time selection never writes track_fields."""
    for lane in ("beatgrid", "key", "loudness"):
        store_mod.upsert_record(own_record(lane=lane), conn=db)
    assert db.execute("SELECT count(*) FROM track_fields").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM track_field_history").fetchone()[0] == 0
    # Positive control: the tables exist, so counting zero is a measurement
    # rather than a missing subject.
    db.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, created_at, "
        "updated_at) VALUES ('probe', 'inferred', 'p', '2026-09-09T00:00:00Z', "
        "'2026-09-09T00:00:00Z')"
    )
    db.execute(
        "INSERT INTO track_fields (stable_id, field_name, value_json, source, "
        "modified_at) VALUES ('probe', 'bpm', '1.0', 'webui', '2026-09-09T00:00:00Z')"
    )
    assert db.execute("SELECT count(*) FROM track_fields").fetchone()[0] == 1
