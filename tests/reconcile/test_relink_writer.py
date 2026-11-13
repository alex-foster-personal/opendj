"""Relink selection, safety checks, reversal log, apply, undo and availability.

Split from ``tests/reconcile/test_relink.py`` for the #1390 file-size gate. Shared
helpers live in :mod:`tests.reconcile.relink_support`.

Ties to RECON-01/02.

"""

from __future__ import annotations

import csv
import inspect
import sqlite3
from pathlib import Path

import pytest

from apps.reconcile import relink
from apps.shared.state import db as state_db_mod
from apps.shared.state.writer import StateWriter
from tests.reconcile.relink_support import (
    _audio,
    _cand,
    _entry,
    _make_state_db,
    _result,
    _rows,
)

# ----- selection --------------------------------------------------------


@pytest.mark.requirement("RECON-02")
def test_selection_keeps_only_requested_bucket_above_confidence() -> None:
    """[if] test_selection_keeps_only_requested_bucket_above_confidence regresses [then] relink contract breaks, [else stop]."""
    results = [
        _result("auto-strong", candidates=[_cand("/disk/a.mp3", 0.95)]),
        _result("auto-weak", candidates=[_cand("/disk/b.mp3", 0.70)]),
        _result(
            "amb",
            bucket="relinkable-ambiguous",
            candidates=[_cand("/disk/c.mp3", 0.95)],
            ambiguity="multiple-above-threshold",
        ),
    ]
    picked = relink.select_entries(results, buckets=["relinkable-auto"], min_confidence=0.85)
    assert [e.stable_id for e in picked] == ["auto-strong"], (
        "if a below-threshold or out-of-bucket row is selected then the "
        "confidence floor and the bucket filter are not being enforced"
    )
    assert picked[0].new_path == "/disk/a.mp3"


@pytest.mark.requirement("RECON-01")
@pytest.mark.parametrize("bucket", ["present", "awaiting-volume", "streaming", "absent-no-audio"])
def test_selection_refuses_non_relinkable_buckets(bucket: str) -> None:
    """[if] test_selection_refuses_non_relinkable_buckets regresses [then] relink contract breaks, [else stop]."""
    with pytest.raises(ValueError, match="never relinkable"):
        relink.select_entries([], buckets=[bucket], min_confidence=0.0)


@pytest.mark.requirement("RECON-01")
def test_selection_refuses_unknown_bucket_rather_than_ignoring_it() -> None:
    """[if] test_selection_refuses_unknown_bucket_rather_than_ignoring_it regresses [then] relink contract breaks, [else stop]."""
    with pytest.raises(ValueError, match="unknown bucket"):
        relink.select_entries([], buckets=["relinkable-maybe"], min_confidence=0.0)


@pytest.mark.requirement("RECON-02")
def test_selection_limit_takes_the_strongest_evidence_first() -> None:
    """[if] test_selection_limit_takes_the_strongest_evidence_first regresses [then] relink contract breaks, [else stop]."""
    results = [
        _result("weaker", path="/gone/1.mp3", candidates=[_cand("/disk/1.mp3", 0.88)]),
        _result("stronger", path="/gone/2.mp3", candidates=[_cand("/disk/2.mp3", 0.95)]),
    ]
    picked = relink.select_entries(results, buckets=["relinkable-auto"], min_confidence=0.85, limit=1)
    assert [e.stable_id for e in picked] == ["stronger"], (
        "if --limit takes an arbitrary row then two runs with the same limit apply different repairs"
    )


@pytest.mark.requirement("RECON-01")
def test_selection_skips_rows_with_no_recorded_path() -> None:
    """[if] test_selection_skips_rows_with_no_recorded_path regresses [then] relink contract breaks, [else stop]."""
    results = [_result("blank", path=None, candidates=[_cand("/disk/a.mp3")])]
    assert relink.select_entries(results, buckets=["relinkable-auto"], min_confidence=0.0) == [], (
        "if a NULL old_path is selected then the change cannot be reversed"
    )


# ----- pre-write safety re-checks ---------------------------------------


@pytest.mark.requirement("RECON-01")
def test_reject_target_that_vanished_since_classification(tmp_path: Path) -> None:
    """[if] test_reject_target_that_vanished_since_classification regresses [then] relink contract breaks, [else stop]."""
    gone = tmp_path / "poof.mp3"
    ok, rejected = relink.reject_unsafe([_entry("a", "/gone/old.mp3", str(gone))], all_recorded_paths=[])
    assert ok == []
    assert "no longer exists" in rejected[0].reason, (
        "if a vanished candidate is applied then state.db gains a fresh dead link"
    )


@pytest.mark.requirement("RECON-01")
def test_reject_target_already_recorded_by_another_row(tmp_path: Path) -> None:
    """[if] test_reject_target_already_recorded_by_another_row regresses [then] relink contract breaks, [else stop]."""
    target = _audio(tmp_path, "shared.mp3")
    ok, rejected = relink.reject_unsafe(
        [_entry("a", "/gone/old.mp3", str(target))],
        all_recorded_paths=["/gone/old.mp3", str(target)],
    )
    assert ok == []
    assert "already records this exact path" in rejected[0].reason, (
        "if a path recorded by another row is applied then two rows share one file"
    )


@pytest.mark.requirement("RECON-01")
def test_reject_two_entries_claiming_one_file(tmp_path: Path) -> None:
    """[if] test_reject_two_entries_claiming_one_file regresses [then] relink contract breaks, [else stop]."""
    target = _audio(tmp_path, "one.mp3")
    ok, rejected = relink.reject_unsafe(
        [
            _entry("a", "/gone/a.mp3", str(target)),
            _entry("b", "/gone/b.mp3", str(target)),
        ],
        all_recorded_paths=["/gone/a.mp3", "/gone/b.mp3"],
    )
    assert [e.stable_id for e in ok] == ["a"]
    assert "already claimed in this batch" in rejected[0].reason


@pytest.mark.requirement("RECON-01")
def test_reject_noop_write_where_new_equals_old(tmp_path: Path) -> None:
    """[if] test_reject_noop_write_where_new_equals_old regresses [then] relink contract breaks, [else stop]."""
    target = _audio(tmp_path, "same.mp3")
    ok, rejected = relink.reject_unsafe([_entry("a", str(target), str(target))], all_recorded_paths=[str(target)])
    assert ok == [] and "equals old path" in rejected[0].reason


# ----- reversal log -----------------------------------------------------


@pytest.mark.requirement("RECON-01")
def test_log_columns_are_exactly_the_specified_six(tmp_path: Path) -> None:
    """[if] test_log_columns_are_exactly_the_specified_six regresses [then] relink contract breaks, [else stop]."""
    out = relink.write_log([_entry("a", "/old.mp3", "/new.mp3")], tmp_path / "log.csv")
    with out.open(encoding="utf-8") as fh:
        header = next(csv.reader(fh))
    assert (
        header
        == list(relink.LOG_COLUMNS)
        == [
            "stable_id",
            "old_path",
            "new_path",
            "tier",
            "confidence",
            "timestamp",
        ]
    )


@pytest.mark.requirement("RECON-01")
def test_log_round_trips_through_read_log(tmp_path: Path) -> None:
    """[if] test_log_round_trips_through_read_log regresses [then] relink contract breaks, [else stop]."""
    entries = [_entry("a", "/old-a.mp3", "/new-a.mp3", 0.9), _entry("b", "/o.mp3", "/n.mp3")]
    out = relink.write_log(entries, tmp_path / "relink-log-x.csv")
    back = relink.read_log(out)
    assert [(e.stable_id, e.old_path, e.new_path) for e in back] == [
        (e.stable_id, e.old_path, e.new_path) for e in entries
    ]


@pytest.mark.requirement("RECON-01")
def test_read_log_refuses_a_dry_run_log(tmp_path: Path) -> None:
    """[if] test_read_log_refuses_a_dry_run_log regresses [then] relink contract breaks, [else stop]."""
    out = relink.write_log(
        [_entry("a", "/old.mp3", "/new.mp3")],
        relink.log_path_for(tmp_path, live=False),
    )
    assert out.name.endswith(relink.DRY_RUN_LOG_SUFFIX)
    with pytest.raises(ValueError, match="DRY-RUN log"):
        relink.read_log(out)


@pytest.mark.requirement("RECON-01")
def test_read_log_raises_on_a_missing_column(tmp_path: Path) -> None:
    """[if] test_read_log_raises_on_a_missing_column regresses [then] relink contract breaks, [else stop]."""
    bad = tmp_path / "relink-log-bad.csv"
    bad.write_text("stable_id,old_path\na,/x.mp3\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing column"):
        relink.read_log(bad)


@pytest.mark.requirement("RECON-01")
def test_read_log_raises_when_absent(tmp_path: Path) -> None:
    """[if] test_read_log_raises_when_absent regresses [then] relink contract breaks, [else stop]."""
    with pytest.raises(FileNotFoundError):
        relink.read_log(tmp_path / "nope.csv")


# ----- the write --------------------------------------------------------


@pytest.mark.requirement("RECON-01")
def test_apply_changes_file_path_and_nothing_else(tmp_path: Path) -> None:
    """[if] test_apply_changes_file_path_and_nothing_else regresses [then] relink contract breaks, [else stop]."""
    target = _audio(tmp_path / "disk", "moved.mp3")
    db = _make_state_db(
        tmp_path / "state.db",
        [
            {
                "stable_id": "a",
                "title": "Quiet Rooms",
                "artists": ["Orla Finch", "JUNO-K"],
                "album": "Palaces",
                "isrc": "GBAYE2101234",
                "duration_ms": 232000,
                "file_path": "/gone/moved.mp3",
                "content_hash": "deadbeef",
            }
        ],
    )
    before = _rows(db)["a"]
    outcomes = relink.apply_entries(db, [_entry("a", "/gone/moved.mp3", str(target))])
    after = _rows(db)["a"]
    assert [o.outcome for o in outcomes] == ["changed"]
    idx = relink.TRACK_COLUMNS.index("file_path")
    assert after[idx] == str(target)
    for pos, col in enumerate(relink.TRACK_COLUMNS):
        if col in relink.MUTABLE_COLUMNS:
            continue
        assert after[pos] == before[pos], f"if a relink changes {col} then it is not a link repair, it is an edit"


@pytest.mark.requirement("RECON-01")
def test_apply_appends_a_track_update_event(tmp_path: Path) -> None:
    """[if] test_apply_appends_a_track_update_event regresses [then] relink contract breaks, [else stop]."""
    target = _audio(tmp_path / "disk", "ev.mp3")
    db = _make_state_db(
        tmp_path / "state.db",
        [{"stable_id": "a", "file_path": "/gone/ev.mp3"}],
    )
    relink.apply_entries(db, [_entry("a", "/gone/ev.mp3", str(target))])
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        kinds = [r[0] for r in conn.execute("SELECT kind FROM events WHERE stable_id = 'a' ORDER BY rowid")]
        actors = {r[0] for r in conn.execute("SELECT actor FROM events WHERE kind='track.update'")}
    finally:
        conn.close()
    assert kinds[-1] == "track.update", "if a relink appends no event then the durable log cannot explain the change"
    assert actors == {"reconcile.relink"}


def test_apply_entries_uses_writer_transaction() -> None:
    src = inspect.getsource(relink.apply_entries)
    assert "writer.transaction(" in src
    assert "SAVEPOINT relink_batch" not in src


@pytest.mark.requirement("RECON-01")
def test_apply_is_idempotent_on_a_second_run(tmp_path: Path) -> None:
    """[if] test_apply_is_idempotent_on_a_second_run regresses [then] relink contract breaks, [else stop]."""
    target = _audio(tmp_path / "disk", "idem.mp3")
    db = _make_state_db(tmp_path / "state.db", [{"stable_id": "a", "file_path": "/gone/idem.mp3"}])
    entries = [_entry("a", "/gone/idem.mp3", str(target))]
    assert [o.outcome for o in relink.apply_entries(db, entries)] == ["changed"]
    snapshot = _rows(db)
    assert [o.outcome for o in relink.apply_entries(db, entries)] == ["already"]
    assert _rows(db) == snapshot, "if a repeat apply rewrites the row then updated_at churns for no change"


@pytest.mark.requirement("RECON-01")
def test_one_conflicting_row_rolls_the_whole_batch_back(tmp_path: Path) -> None:
    """[if] test_one_conflicting_row_rolls_the_whole_batch_back regresses [then] relink contract breaks, [else stop]."""
    good = _audio(tmp_path / "disk", "good.mp3")
    bad = _audio(tmp_path / "disk", "bad.mp3")
    db = _make_state_db(
        tmp_path / "state.db",
        [
            {"stable_id": "a", "file_path": "/gone/good.mp3"},
            {"stable_id": "b", "file_path": "/somewhere/else.mp3"},
        ],
    )
    before = _rows(db)
    entries = [
        _entry("a", "/gone/good.mp3", str(good)),
        _entry("b", "/gone/bad.mp3", str(bad)),  # old_path does not match the DB
    ]
    with pytest.raises(RuntimeError, match="drifted"):
        relink.apply_entries(db, entries)
    assert _rows(db) == before, (
        "if a failed batch leaves row a applied then there is no single point "
        "to roll back to and the log describes a state that never existed"
    )


@pytest.mark.requirement("RECON-01")
def test_missing_stable_id_is_a_conflict_not_an_insert(tmp_path: Path) -> None:
    """[if] test_missing_stable_id_is_a_conflict_not_an_insert regresses [then] relink contract breaks, [else stop]."""
    target = _audio(tmp_path / "disk", "ghost.mp3")
    db = _make_state_db(tmp_path / "state.db", [{"stable_id": "a", "file_path": "/x.mp3"}])
    before = _rows(db)
    with pytest.raises(RuntimeError, match="drifted"):
        relink.apply_entries(db, [_entry("ghost", "/gone/ghost.mp3", str(target))])
    assert _rows(db) == before, "if an unknown stable_id inserts a row then relink can invent tracks"


@pytest.mark.requirement("RECON-01")
def test_drift_guard_rolls_back_when_another_column_moves(tmp_path: Path, monkeypatch) -> None:
    """[if] test_drift_guard_rolls_back_when_another_column_moves regresses [then] relink contract breaks, [else stop]."""
    """The guard, not the writer, is what makes 'only file_path' true."""
    target = _audio(tmp_path / "disk", "guard.mp3")
    db = _make_state_db(
        tmp_path / "state.db",
        [{"stable_id": "a", "title": "Keep Me", "file_path": "/gone/guard.mp3"}],
    )
    before = _rows(db)
    real_upsert = StateWriter.upsert_track

    def sneaky(self, **kw):
        kw["title"] = "Clobbered"
        return real_upsert(self, **kw)

    monkeypatch.setattr(StateWriter, "upsert_track", sneaky)
    with pytest.raises(RuntimeError, match="beyond file_path"):
        relink.apply_entries(db, [_entry("a", "/gone/guard.mp3", str(target))])
    assert _rows(db) == before


# ----- undo -------------------------------------------------------------


@pytest.mark.requirement("RECON-01")
def test_undo_restores_every_row_byte_for_byte_except_updated_at(
    tmp_path: Path,
) -> None:
    """[if] test_undo_restores_every_row_byte_for_byte_except_updated_at regresses [then] relink contract breaks, [else stop]."""
    target = _audio(tmp_path / "disk", "back.mp3")
    db = _make_state_db(
        tmp_path / "state.db",
        [
            {
                "stable_id": "a",
                "title": "Round Trip",
                "artists": ["A", "B"],
                "album": "LP",
                "isrc": "GBAYE2101234",
                "duration_ms": 1000,
                "file_path": "/gone/back.mp3",
                "content_hash": "cafe",
            }
        ],
    )
    before = _rows(db)["a"]
    entries = [_entry("a", "/gone/back.mp3", str(target))]
    relink.apply_entries(db, entries)
    relink.apply_entries(db, entries, reverse=True, tolerate_conflicts=True)
    after = _rows(db)["a"]
    for pos, col in enumerate(relink.TRACK_COLUMNS):
        if col == "updated_at":
            continue
        assert after[pos] == before[pos], f"if undo does not restore {col} then the repair is not reversible"


@pytest.mark.requirement("RECON-01")
def test_undo_twice_reports_already_and_changes_nothing(tmp_path: Path) -> None:
    """[if] test_undo_twice_reports_already_and_changes_nothing regresses [then] relink contract breaks, [else stop]."""
    target = _audio(tmp_path / "disk", "twice.mp3")
    db = _make_state_db(tmp_path / "state.db", [{"stable_id": "a", "file_path": "/gone/twice.mp3"}])
    entries = [_entry("a", "/gone/twice.mp3", str(target))]
    relink.apply_entries(db, entries)
    relink.apply_entries(db, entries, reverse=True, tolerate_conflicts=True)
    snapshot = _rows(db)
    outcomes = relink.apply_entries(db, entries, reverse=True, tolerate_conflicts=True)
    assert [o.outcome for o in outcomes] == ["already"]
    assert _rows(db) == snapshot


@pytest.mark.requirement("RECON-01")
def test_undo_leaves_a_drifted_row_alone_and_reverts_the_rest(tmp_path: Path) -> None:
    """[if] test_undo_leaves_a_drifted_row_alone_and_reverts_the_rest regresses [then] relink contract breaks, [else stop]."""
    one = _audio(tmp_path / "disk", "one.mp3")
    two = _audio(tmp_path / "disk", "two.mp3")
    db = _make_state_db(
        tmp_path / "state.db",
        [
            {"stable_id": "a", "file_path": "/gone/one.mp3"},
            {"stable_id": "b", "file_path": "/gone/two.mp3"},
        ],
    )
    entries = [
        _entry("a", "/gone/one.mp3", str(one)),
        _entry("b", "/gone/two.mp3", str(two)),
    ]
    relink.apply_entries(db, entries)
    # Someone else edits row b out from under us.
    conn = state_db_mod.open_rw(db, apply_schema=False)
    try:
        conn.execute("UPDATE tracks SET file_path = '/third/party.mp3' WHERE stable_id='b'")
    finally:
        conn.close()
    outcomes = relink.apply_entries(db, entries, reverse=True, tolerate_conflicts=True)
    by_id = {o.stable_id: o for o in outcomes}
    assert by_id["a"].outcome == "changed"
    assert by_id["b"].outcome == "conflict", "if undo overwrites a third-party edit then it destroys work it did not do"
    rows = _rows(db)
    idx = relink.TRACK_COLUMNS.index("file_path")
    assert rows["a"][idx] == "/gone/one.mp3"
    assert rows["b"][idx] == "/third/party.mp3"


# ----- availability -----------------------------------------------------


@pytest.mark.requirement("RECON-01")
def test_availability_counts_present_and_offline_volumes(tmp_path: Path) -> None:
    """[if] test_availability_counts_present_and_offline_volumes regresses [then] relink contract breaks, [else stop]."""
    here = _audio(tmp_path / "disk", "here.mp3")
    db = _make_state_db(
        tmp_path / "state.db",
        [
            {"stable_id": "a", "file_path": str(here)},
            {"stable_id": "b", "file_path": "/gone/x.mp3"},
            {"stable_id": "c", "file_path": "/Volumes/NOPE-NOT-MOUNTED/x.mp3"},
        ],
    )
    avail = relink.measure_availability(db)
    assert (avail.total, avail.present, avail.missing) == (3, 1, 2)
    assert avail.awaiting_volume == 1, (
        "if an unmounted-volume row is not counted separately then the before/after delta looks like data loss"
    )
