"""effective_fields: the one scalar read, wired into the track read model.

[if] an own lane serves rekordbox data or writes track_fields [then] fail, [else stop].

Spec: `specs/native-analysis-v1.md` section 3 ("Consumer, key and loudness
lanes"). Requirements: NATIVE-04, NATIVE-07.

Acceptance lines exercised here:
- [if] own is selected for a lane [then] that lane serves own data or a named
  status, never rekordbox data silently.
- [if] own key is `failed` [then] the row carries status failed and the reason,
  never a blank cell that reads as ordinary missing metadata.
- [if] any own producer, toggle or promotion runs [then] nothing writes an own
  value into track_fields or track_field_history.
- [if] the track list and the smartlist evaluator are both asked for one
  track's effective key [then] they return the same value.

-Claude
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from apps.analysis import selection
from apps.analysis import store as analysis_store
from apps.analysis.lanes import LaneResult
from apps.analysis.record import AnalysisRecord
from apps.smartlists.evaluator import evaluate
from apps.webui.server import sqlite_backend as sb

pytestmark = pytest.mark.requirement("NATIVE-04")

STAMP = "2026-09-09T00:00:00Z"
# A real digest: the record contract checks the SHAPE of this field.
DECODE_FINGERPRINT = (
    "sha256:663de53948b9c9d36e558f3db58a301677b498eb67da9637338ea0ba296965e8"
)


#: A stand-in for the beatgrid this segments block was computed against
#: (spec section 5): this suite is about the scalar key projection, not
#: staleness, so one fixed identity is enough to satisfy the record contract.
_DEPENDS_ON_BEATGRID = {
    "backend": "own_beatgrid.backfill", "producer_version": "1.0.0",
    "model_sha256": None, "decode_fingerprint": "sha256:" + "1f" * 32,
    "record_digest": "sha256:" + "2f" * 32,
}


def _key_payload(camelot: str, segments: int = 1) -> dict:
    """Derived, not hardcoded: the contract cross-checks the three spellings."""
    from apps.analysis.lane_payloads import _CAMELOT_PITCH_CLASS

    pitch_class, is_minor = _CAMELOT_PITCH_CLASS[camelot]
    return {
        "camelot": camelot, "openkey": "1m", "pitch_class": pitch_class,
        "is_minor": is_minor, "confidence": 0.8,
        "segments": {
            "status": "ok", "reason": None,
            "segments": [
                {"start_bar": i * 8, "end_bar": (i + 1) * 8,
                 "start_s": i * 16.0, "end_s": (i + 1) * 16.0,
                 "key_camelot": camelot, "key_openkey": "1m", "confidence": 0.8}
                for i in range(segments)
            ],
        },
        "depends_on": {"beatgrid": dict(_DEPENDS_ON_BEATGRID)},
    }


def _own_key_record(stable_id: str, result: LaneResult) -> AnalysisRecord:
    return AnalysisRecord(
        stable_id=stable_id, backend="own_key.inapp", backend_version="1.0.0",
        analyzed_at=datetime(2026, 9, 9, tzinfo=UTC),
        duration_s=200.0, sample_rate=44100,
        bpm=120.0, bpm_confidence=0.9,
        key_camelot="8A", key_openkey="1m", key_confidence=0.8,
        energy=5, energy_source="inferred",
        producer="inapp", producer_version="1.0.0", uses_model=False,
        model_sha256=None,
                decode_fingerprint=DECODE_FINGERPRINT,
        lanes={"key": result},
    )


@pytest.fixture()
def state(tmp_path):
    """A state.db with one track carrying a rekordbox key in track_fields."""
    path = tmp_path / "state.db"
    conn = analysis_store.open_conn(path)
    conn.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, created_at, updated_at) "
        "VALUES ('t1', 'inferred', 'Fixture', ?, ?)", (STAMP, STAMP),
    )
    conn.execute(
        "INSERT INTO track_fields (stable_id, field_name, value_json, source, "
        "confidence, modified_at) VALUES ('t1', 'key', '\"5A\"', 'rekordbox', 1.0, ?)",
        (STAMP,),
    )
    conn.execute(
        "INSERT INTO track_fields (stable_id, field_name, value_json, source, "
        "confidence, modified_at) VALUES ('t1', 'bpm', '124.0', 'rekordbox', 1.0, ?)",
        (STAMP,),
    )
    yield path, conn
    conn.close()


@pytest.fixture(autouse=True)
def _clean_toggles():
    selection.reset_toggles()
    yield
    selection.reset_toggles()


#-----------------------------------------------------------------------------
# rbx vs own
#-----------------------------------------------------------------------------

def test_rbx_lane_serves_the_rekordbox_value(state) -> None:
    _, conn = state
    fields = selection.effective_fields(conn, ["t1"], selection.Selection.all_rbx())
    assert fields["t1"]["key"].value == "5A"
    assert fields["t1"]["key"].source == "rekordbox"
    assert fields["t1"]["key"].status == "ok"
    # rekordbox has no loudness or change counts, so those fields are ABSENT
    # rather than present-and-null.
    assert "loudness_lufs" not in fields["t1"]


def test_own_lane_serves_the_projection_not_the_rekordbox_value(state) -> None:
    _, conn = state
    analysis_store.upsert_record(
        _own_key_record("t1", LaneResult(status="ok", payload=_key_payload("8A"))),
        conn=conn,
    )
    selection.set_toggle("key", "own")
    fields = selection.effective_fields(conn, ["t1"], selection.Selection.resolve(conn))
    assert fields["t1"]["key"].value == "8A"
    assert fields["t1"]["key"].source == "own_key.inapp"


def test_own_lane_with_no_record_reads_missing_not_the_rekordbox_value(state) -> None:
    """The whole point: no silent substitution when own has nothing."""
    _, conn = state
    selection.set_toggle("key", "own")
    fields = selection.effective_fields(conn, ["t1"], selection.Selection.resolve(conn))
    assert fields["t1"]["key"].status == "missing"
    assert fields["t1"]["key"].value is None
    assert fields["t1"]["key"].value != "5A"


def test_a_failed_own_lane_carries_status_and_reason(state) -> None:
    _, conn = state
    analysis_store.upsert_record(
        _own_key_record("t1", LaneResult(status="failed", reason="no_tonal_center")),
        conn=conn,
    )
    selection.set_toggle("key", "own")
    fields = selection.effective_fields(conn, ["t1"], selection.Selection.resolve(conn))
    assert fields["t1"]["key"].status == "failed"
    assert fields["t1"]["key"].reason == "no_tonal_center"
    assert fields["t1"]["key"].value is None


#-----------------------------------------------------------------------------
# wired into the track read model
#-----------------------------------------------------------------------------

def test_the_track_read_model_serves_the_own_key_and_its_status(state) -> None:
    path, conn = state
    analysis_store.upsert_record(
        _own_key_record("t1", LaneResult(status="ok", payload=_key_payload("8A", 3))),
        conn=conn,
    )
    selection.set_toggle("key", "own")
    backend = sb.SqliteBackend(path)
    track = backend.get_track("t1")
    assert track.key == "8A"
    assert track.provenance["key"].status == "ok"
    assert track.provenance["key"].source == "own_key.inapp"
    assert track.provenance["key_change_count"].value == 2


def test_the_track_read_model_is_unchanged_while_every_lane_is_rbx(
    state, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive control: the override must be conditional, not unconditional."""
    from apps.webui.server.rb_vendor_pkg import track_rows

    class _Meta:
        vendor_id = "v1"

    monkeypatch.setattr(
        track_rows, "bulk_rb_meta", lambda stable_ids: {sid: _Meta() for sid in stable_ids}
    )
    path, _conn = state
    backend = sb.SqliteBackend(path)
    track = backend.get_track("t1")
    assert track.key == "5A"
    assert track.bpm == 124.0
    assert track.provenance["key"].source == "rekordbox"
    assert track.provenance["key"].status == "ok"


def test_a_failed_own_lane_reaches_provenance_rather_than_blanking_the_cell(
    state,
) -> None:
    path, conn = state
    analysis_store.upsert_record(
        _own_key_record("t1", LaneResult(status="failed", reason="no_tonal_center")),
        conn=conn,
    )
    selection.set_toggle("key", "own")
    track = sb.SqliteBackend(path).get_track("t1")
    assert track.key is None
    assert track.provenance["key"].status == "failed"
    assert track.provenance["key"].reason == "no_tonal_center"


def test_reading_through_own_never_writes_track_fields_or_history(state) -> None:
    path, conn = state
    analysis_store.upsert_record(
        _own_key_record("t1", LaneResult(status="ok", payload=_key_payload("8A"))),
        conn=conn,
    )
    selection.set_default(conn, "key", "own")
    selection.set_toggle("key", "own")
    before = conn.execute(
        "SELECT field_name, value_json FROM track_fields ORDER BY field_name"
    ).fetchall()
    history_before = conn.execute(
        "SELECT count(*) FROM track_field_history"
    ).fetchone()[0]

    backend = sb.SqliteBackend(path)
    backend.get_track("t1")
    backend.list_tracks(sb.TrackFilter())

    check = sqlite3.connect(path)
    try:
        assert check.execute(
            "SELECT field_name, value_json FROM track_fields ORDER BY field_name"
        ).fetchall() == before
        assert check.execute(
            "SELECT count(*) FROM track_field_history"
        ).fetchone()[0] == history_before
        # Negative control: the rekordbox key is still sitting in track_fields,
        # so "unchanged" is a comparison of real rows and not of two empties.
        assert ("key", '"5A"') in before
    finally:
        check.close()


#-----------------------------------------------------------------------------
# the two readers agree
#-----------------------------------------------------------------------------

def test_track_list_and_smartlist_agree_on_the_effective_key(state) -> None:
    path, conn = state
    analysis_store.upsert_record(
        _own_key_record("t1", LaneResult(status="ok", payload=_key_payload("8A"))),
        conn=conn,
    )
    selection.set_default(conn, "key", "own")

    track = sb.SqliteBackend(path).get_track("t1")
    assert track.key == "8A"

    # The smartlist resolves through the same module and the same Selection.
    assert evaluate({"field": "key", "op": "=", "value": "8A"}, conn) == ["t1"]
    # And the rekordbox value must NOT match while the lane is own.
    assert evaluate({"field": "key", "op": "=", "value": "5A"}, conn) == []


def test_smartlist_can_filter_on_the_own_only_fields(state) -> None:
    """NATIVE-07: loudness is consumed, not merely produced."""
    _, conn = state
    conn.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, created_at, updated_at) "
        "VALUES ('t2', 'inferred', 'Quiet', ?, ?)", (STAMP, STAMP),
    )
    for sid, lufs in (("t1", -6.0), ("t2", -14.0)):
        analysis_store.upsert_record(
            AnalysisRecord(
                stable_id=sid, backend="own_loudness.backfill",
                backend_version="1.0.0",
                analyzed_at=datetime(2026, 9, 9, tzinfo=UTC),
                duration_s=200.0, sample_rate=44100, bpm=120.0, bpm_confidence=0.9,
                key_camelot="8A", key_openkey="1m", key_confidence=0.8,
                energy=5, energy_source="inferred",
                producer="backfill", producer_version="1.0.0", uses_model=False,
                model_sha256=None, decode_fingerprint=DECODE_FINGERPRINT,
                lanes={"loudness": LaneResult(status="ok", payload={
                    "integrated_lufs": lufs, "true_peak_dbtp": -0.2,
                    "loudness_range_lu": 6.0, "rms_db": -12.0,
                })},
            ),
            conn=conn,
        )
    selection.set_default(conn, "loudness", "own")
    assert evaluate({"field": "loudness_lufs", "op": ">", "value": -10.0}, conn) == ["t1"]

    # Negative control: with the lane back on rbx, rekordbox has no such
    # column, so the filter must match NOTHING rather than silently
    # resolving to some other value.
    selection.set_default(conn, "loudness", "rbx")
    assert evaluate({"field": "loudness_lufs", "op": ">", "value": -10.0}, conn) == []


#-----------------------------------------------------------------------------
# the defect a live run caught and the unit tests did not
#-----------------------------------------------------------------------------

def test_own_lane_reads_missing_even_when_the_projection_table_does_not_exist(
    tmp_path,
) -> None:
    """A promoted lane on a database with no own record must NOT serve rekordbox.

    Found live on Wed 9 Sep 2026, not by a unit test: the defaults live in
    `analysis_source_default` and the values in `analysis_projection`, so a
    lane can be on own while the projection table is still absent. Gating
    the override on that table's existence served the rekordbox value under
    an `own` selection. The fixtures in this file all use
    `analysis_store.open_conn`, which creates BOTH tables, so none of them
    could reach the state.
    """
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path)
    from apps.shared.state import db as state_db

    state_db.open_rw(path).close()
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO tracks (stable_id, stable_id_tier, title, created_at, updated_at) "
        "VALUES ('t1', 'inferred', 'Fixture', ?, ?)", (STAMP, STAMP),
    )
    conn.execute(
        "INSERT INTO track_fields (stable_id, field_name, value_json, source, "
        "confidence, modified_at) VALUES ('t1', 'key', '\"5A\"', 'rekordbox', 1.0, ?)",
        (STAMP,),
    )
    # Written with raw SQL, not `selection.set_default`: that function now
    # provisions the whole analysis schema (the writer half of the same
    # finding) and so can no longer produce this state. It is still reachable
    # in the wild -- a database promoted by an older build -- and the READER
    # must be hardened against it independently, or fixing the writer would
    # silently retire the test for the reader.
    conn.execute(
        "CREATE TABLE analysis_source_default (lane TEXT PRIMARY KEY, "
        "source TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO analysis_source_default VALUES ('key', 'own', ?)", (STAMP,)
    )
    conn.commit()
    # The precondition the defect depended on, asserted rather than assumed.
    assert conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE name='analysis_projection'"
    ).fetchone()[0] == 0
    assert selection.effective_source(conn, "key") == "own"
    conn.close()

    track = sb.SqliteBackend(path).get_track("t1")
    assert track.key is None
    assert track.provenance["key"].status == "missing"


def test_all_rbx_skip_returns_exactly_what_effective_fields_would(
    state, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The listing hot path skips the projection query under all-rbx.

    That skip is only safe if it is a no-op, so the claim is measured
    rather than argued: the same connection, both paths, same answer.
    """
    from apps.webui.server import sqlite_backend as backend_mod
    from apps.webui.server.rb_vendor_pkg import track_rows

    class _Meta:
        vendor_id = "v1"

    monkeypatch.setattr(
        track_rows, "bulk_rb_meta", lambda stable_ids: {sid: _Meta() for sid in stable_ids}
    )

    _, conn = state
    conn.row_factory = sqlite3.Row  # _fetch_fields reads rows by column name
    skipped = backend_mod._lane_owned_fields(conn, ["t1"])
    assert skipped == {}
    computed = selection.effective_fields(
        conn, ["t1"], selection.Selection.all_rbx()
    )
    eav = {
        name: view for name, view in
        backend_mod._fetch_fields(conn, ["t1"])["t1"].items()
        if name in computed["t1"]
    }
    assert eav == computed["t1"]


#-----------------------------------------------------------------------------
# P2 round 5: the etag must separate two representations of one track
#-----------------------------------------------------------------------------

def test_switching_a_lane_to_own_changes_the_track_etag(
    state, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reproduced: the projection row can be OLDER than the base updated_at.

    The etag is the maximum of the base stamp and every field stamp, so an
    own record written with an earlier stamp moves nothing, and the same
    strong validator would then cover a track serving `5A` and the same
    track serving `8A` -- letting a stale If-Match through.
    """
    from apps.webui.server.etag import compute_etag
    from apps.webui.server.rb_vendor_pkg import track_rows

    class _Meta:
        vendor_id = "v1"

    monkeypatch.setattr(
        track_rows, "bulk_rb_meta", lambda stable_ids: {sid: _Meta() for sid in stable_ids}
    )
    path, conn = state
    backend = sb.SqliteBackend(path)
    before = backend.get_track("t1")
    etag_before = compute_etag(before.stable_id, before.updated_at, before.selection_tag)
    assert before.key == "5A"

    analysis_store.upsert_record(
        _own_key_record("t1", LaneResult(status="ok", payload=_key_payload("8A"))),
        conn=conn,
    )
    # Force the exact collision the finding describes: an own projection row
    # stamped BEFORE the track's base updated_at.
    conn.execute(
        "UPDATE analysis_projection SET updated_at = '2020-01-01T00:00:00Z'"
    )
    conn.commit()
    selection.set_toggle("key", "own")

    after = sb.SqliteBackend(path).get_track("t1")
    assert after.key == "8A"
    assert after.updated_at == before.updated_at, (
        "precondition: the timestamps must be identical, or this proves nothing"
    )
    etag_after = compute_etag(after.stable_id, after.updated_at, after.selection_tag)
    assert etag_after != etag_before


def test_an_all_rbx_track_keeps_a_byte_identical_etag(
    state, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: the variant must be EMPTY while nothing is promoted.

    Every etag in the wild today is `sha1(stable_id:updated_at)`, so an
    unconditional variant would invalidate every client cache on deploy.
    """
    from apps.webui.server.etag import compute_etag
    from apps.webui.server.rb_vendor_pkg import track_rows

    class _Meta:
        vendor_id = "v1"

    monkeypatch.setattr(
        track_rows, "bulk_rb_meta", lambda stable_ids: {sid: _Meta() for sid in stable_ids}
    )
    path, _ = state
    track = sb.SqliteBackend(path).get_track("t1")
    assert track.selection_tag == ""
    assert compute_etag(
        track.stable_id, track.updated_at, track.selection_tag
    ) == compute_etag(track.stable_id, track.updated_at)


def test_a_producer_version_bump_changes_the_etag_even_with_a_future_base_stamp(
    state,
) -> None:
    """The etag must move when the own VALUE moves, not only when the SOURCE does.

    `_effective_updated_at` takes the MAXIMUM, so a `tracks.updated_at` from a
    host with a fast clock sits above every projection stamp and a
    producer-version bump could rewrite the own key while the selected maximum
    never budged. The variant carries the projection's own timestamp for that
    reason (Codex P2, PR #1549).
    """
    from apps.webui.server.etag import compute_etag

    path, conn = state
    conn.execute("UPDATE tracks SET updated_at = '2099-01-01T00:00:00Z'")
    analysis_store.upsert_record(
        _own_key_record("t1", LaneResult(status="ok", payload=_key_payload("8A"))),
        conn=conn,
    )
    conn.commit()
    selection.set_toggle("key", "own")

    first = sb.SqliteBackend(path).get_track("t1")
    etag_first = compute_etag(first.stable_id, first.updated_at, first.selection_tag)
    assert first.key == "8A"

    # Same producer, same source, NEW version and a new value.
    import dataclasses

    bumped = dataclasses.replace(
        _own_key_record("t1", LaneResult(status="ok", payload=_key_payload("9A"))),
        backend_version="2.0.0", producer_version="2.0.0",
    )
    analysis_store.upsert_record(bumped, conn=conn)
    conn.commit()

    second = sb.SqliteBackend(path).get_track("t1")
    assert second.key == "9A"
    assert second.updated_at == first.updated_at, (
        "precondition: the future base stamp must still win the maximum"
    )
    assert compute_etag(
        second.stable_id, second.updated_at, second.selection_tag
    ) != etag_first
