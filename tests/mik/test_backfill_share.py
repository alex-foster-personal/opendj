"""``backfill share``: a complete donor's MIK energy reaches its empty duplicates (MIKBF-08).

- [if] a row of a duplicate group has a MIK energy AND series and another present row has neither [then] share writes both to it, [else stop].
- [if] the group's complete donors disagree [then] nothing is written and the group is reported conflicting, [else stop].
- [if] the only donor has a scalar without a series [then] it is no donor and nothing is shared, [else stop].
- [if] share runs live twice [then] the second run writes nothing, [else stop].
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from apps.mik import backfill, backfill_share
from apps.mik import load as loader
from tests.mik.backfill_support import SEGMENTS, Lib, sid

pytestmark = pytest.mark.requirement("MIKBF-08")


def _same_file(lib: Lib, *letters: str) -> None:
    """Rows that all resolve to ONE file on disk: one recording."""
    for letter in letters:
        lib.track(letter, "Music/x.mp3", stored=f"/Users/old/{letter}/x.mp3")


def _mik_energy(lib: Lib, letter: str) -> tuple[object, ...] | None:
    return lib.conn.execute(
        "SELECT value_json, source FROM track_fields WHERE stable_id = ? AND field_name = 'energy'", (sid(letter),)
    ).fetchone()


def _mik_series(lib: Lib, letter: str) -> list[tuple[object, ...]]:
    return lib.conn.execute(
        "SELECT seq, start_ms, length_ms, energy FROM track_energy_segments "
        "WHERE stable_id = ? AND source = 'mik' ORDER BY seq",
        (sid(letter),),
    ).fetchall()


def _without(payload: dict) -> dict:
    return payload["without_mik_energy"]


def test_share_fills_an_empty_duplicate_from_a_complete_donor_once(lib: Lib, write_verdicts) -> None:
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    _same_file(lib, "a", "b")
    lib.energy("a", 7)
    lib.series("a")
    assert _without(backfill.status(lib.library))["would_gain_by_duplicate_sharing"] == 1

    dry = backfill_share.share(lib.library)
    assert dry["mode"] == "dry-run" and dry["counts"]["rows_to_fill"] == 1
    assert dry["groups"] == [{"donors": [sid("a")], "energy": 7, "fills": [sid("b")], "conflicting": False}]
    assert dry["applied"] is None and _mik_energy(lib, "b") is None

    live = backfill_share.share(lib.library, live=True)
    assert live["applied"]["fields_written"] == 1 and live["applied"]["segment_tracks"] == 1
    assert _mik_energy(lib, "b") == ("7", "mik")
    assert _mik_series(lib, "b") == _mik_series(lib, "a") != []
    after = backfill.status(lib.library)
    assert after["with_mik_energy"] == 2 and _without(after)["would_gain_by_duplicate_sharing"] == 0

    again = backfill_share.share(lib.library, live=True)
    assert again["counts"]["rows_to_fill"] == 0
    assert again["applied"]["fields_written"] == 0 and again["applied"]["segment_tracks"] == 0


def test_a_donor_whose_series_is_from_an_older_analysis_shares_nothing(lib: Lib, write_verdicts) -> None:
    """if a MIK scalar paired with a stale series from an older analysis is shared to its duplicates then broken."""
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    _same_file(lib, "a", "b")
    lib.energy("a", 7)
    lib.series("a", modified_at="2025-06-01T00:00:00+00:00")  # left over from an older analysis
    assert _without(backfill.status(lib.library))["would_gain_by_duplicate_sharing"] == 0
    payload = backfill_share.share(lib.library, live=True)
    assert payload["counts"]["rows_to_fill"] == 0 and payload["applied"]["fields_written"] == 0
    assert _mik_energy(lib, "b") is None
    lib.conn.execute("DELETE FROM track_energy_segments WHERE stable_id = ?", (sid("a"),))
    lib.series("a")  # control: the same analysis as its scalar, so it shares
    assert backfill_share.share(lib.library, live=True)["applied"]["fields_written"] == 1

def test_disagreeing_donors_share_nothing(lib: Lib, write_verdicts) -> None:
    """Control: two complete donors with different energies settle nothing."""
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    _same_file(lib, "a", "b", "c")
    lib.energy("a", 7)
    lib.series("a")
    lib.energy("c", 4)
    lib.series("c")
    payload = backfill_share.share(lib.library, live=True)
    assert payload["counts"]["rows_to_fill"] == 0 and payload["counts"]["conflicting_groups"] == 1
    assert payload["groups"][0]["conflicting"] is True
    assert payload["applied"]["fields_written"] == 0
    assert _mik_energy(lib, "b") is None and _mik_series(lib, "b") == []


def test_a_scalar_without_its_series_is_no_donor(lib: Lib, write_verdicts) -> None:
    """Control: an incomplete row offers nothing, so its duplicate still needs an analysis."""
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    _same_file(lib, "a", "b")
    lib.energy("a", 7)
    status = backfill.status(lib.library)
    assert _without(status)["would_gain_by_duplicate_sharing"] == 0
    payload = backfill_share.share(lib.library, live=True)
    assert payload["counts"] == {"donors": 0, "groups": 0, "rows_to_fill": 0, "conflicting_groups": 0, "held_for_pending_analysis": 0}
    assert _mik_energy(lib, "b") is None


def test_share_never_touches_a_row_with_its_own_value(lib: Lib, write_verdicts) -> None:
    """Control: a member with an energy of any source is not a row to fill."""
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    _same_file(lib, "a", "b")
    lib.energy("a", 7)
    lib.series("a")
    lib.energy("b", 3, source="manual")
    payload = backfill_share.share(lib.library, live=True)
    assert payload["counts"]["rows_to_fill"] == 0
    assert _mik_energy(lib, "b") == ("3", "manual") and _mik_series(lib, "b") == []


def test_share_writes_nothing_while_the_gate_is_closed(lib: Lib) -> None:
    _same_file(lib, "a", "b")
    lib.energy("a", 7)
    lib.series("a")
    payload = backfill_share.share(lib.library, live=True)
    assert payload["equivalence"] == {"energy": "untested", "energy_segments": "untested"}
    assert payload["applied"]["fields_written"] == 0 and _mik_energy(lib, "b") is None


def test_status_sharing_is_what_share_fills_and_staged_sharing_is_reported_apart(
    lib: Lib, make_mik_store, write_verdicts, capsys
) -> None:
    """if status labels a row 'share' that share cannot fill, or hides rows only a re-load can fill, then broken."""
    from apps.mik import cli

    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    for letter, folder in (("a", "one"), ("b", "two")):
        lib.track(letter, f"{folder}/x.mp3", title="Tamborel", artist="Quilla Ventrosk", duration_ms=200_000)
    for letter in ("e", "f"):  # one file behind two rows, a different name from the staged song's
        lib.track(letter, "Music/z.mp3", stored=f"/Users/old/{letter}/z.mp3")
    store = make_mik_store([{"path": "/gone/x.mp3", "energy": 7.0, "segments": SEGMENTS}])
    load = ["load", "--json", "--live", "--data-dir", str(lib.library.data_dir), "--store", str(store)]
    assert cli.main(load) == 0  # stages the song: its candidates a and b are two rows
    lib.energy("e", 5)
    lib.series("e")
    capsys.readouterr()

    status = _without(backfill.status(lib.library, share_across_duplicates=True))
    dry = backfill_share.share(lib.library, share_across_duplicates=True)
    assert status["would_gain_by_duplicate_sharing"] == dry["counts"]["rows_to_fill"] == 1  # row f
    assert status["would_gain_by_loading_shared_analysis"] == 2  # rows a and b: staged, no donor

    assert cli.main([*load, "--share-across-duplicates"]) == 0  # the named action for them
    capsys.readouterr()
    after = _without(backfill.status(lib.library, share_across_duplicates=True))
    assert after["would_gain_by_loading_shared_analysis"] == 0
    assert after["would_gain_by_duplicate_sharing"] == 1  # f still waits for share


def test_a_direct_store_match_outranks_a_donor_conflict(lib: Lib, make_mik_store) -> None:
    """if a row a pending store binds directly is bucketed as conflicting (and so planned again) then broken."""
    _same_file(lib, "a", "b", "c")
    lib.energy("a", 7)
    lib.series("a")
    lib.energy("c", 4)
    lib.series("c")  # a and c disagree, so sharing to b is a conflict
    assert _without(backfill.status(lib.library))["conflicting"] == 1  # control: no store
    store = make_mik_store([{"path": "/Users/old/b/x.mp3", "energy": 5.0, "segments": SEGMENTS}])
    with_store = backfill.plan(lib.library, store=store, ingest_root=lib.ingest)
    assert _without(with_store)["would_gain_by_unique_match"] == 1
    assert _without(with_store)["conflicting"] == 0
    assert sid("b") not in {stable_id for entry in with_store["next"] for stable_id in entry["stable_ids"]}


def test_a_direct_store_match_outranks_a_library_donor(lib: Lib, make_mik_store) -> None:
    """if status tells the operator to share a donor's value over a pending direct analysis of that row then broken."""
    _same_file(lib, "a", "b")
    lib.energy("a", 7)
    lib.series("a")
    assert _without(backfill.status(lib.library))["would_gain_by_duplicate_sharing"] == 1  # control: no store
    store = make_mik_store([{"path": "/Users/old/b/x.mp3", "energy": 4.0, "segments": SEGMENTS}])
    with_store = backfill.plan(lib.library, store=store, ingest_root=lib.ingest)
    assert _without(with_store)["would_gain_by_unique_match"] == 1
    assert _without(with_store)["would_gain_by_duplicate_sharing"] == 0


def test_a_pending_shared_analysis_that_disagrees_with_a_donor_is_a_conflict(
    lib: Lib, make_mik_store, write_verdicts, capsys
) -> None:
    """if status sends a row to `backfill share` while a pending analysis offers it a different value then broken."""
    from apps.mik import cli

    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    for letter, folder in (("a", "one"), ("b", "two"), ("c", "three")):
        lib.track(letter, f"{folder}/x.mp3", title="Tamborel", artist="Quilla Ventrosk", duration_ms=200_000)
    store = make_mik_store([{"path": "/gone/x.mp3", "energy": 7.0, "segments": SEGMENTS}])
    load = ["load", "--json", "--live", "--data-dir", str(lib.library.data_dir), "--store", str(store)]
    assert cli.main(load) == 0  # stages the song: ambiguous over a, b and c
    capsys.readouterr()
    lib.energy("c", 7)
    lib.series("c")  # control: a donor that AGREES with the pending analysis
    agree = _without(backfill.status(lib.library, share_across_duplicates=True))
    assert agree["would_gain_by_duplicate_sharing"] == 2 and agree["conflicting"] == 0
    lib.conn.execute("UPDATE track_fields SET value_json = '4' WHERE stable_id = ? AND field_name = 'energy'", (sid("c"),))
    lib.conn.commit()
    disagree = _without(backfill.status(lib.library, share_across_duplicates=True))
    assert disagree["would_gain_by_duplicate_sharing"] == 0
    assert disagree["conflicting"] == 2


def _staged_twins(lib: Lib, make_mik_store, letters: tuple[str, ...], capsys) -> None:
    from apps.mik import cli

    for letter, folder in zip(letters, ("one", "two", "three"), strict=False):
        lib.track(letter, f"{folder}/x.mp3", title="Tamborel", artist="Quilla Ventrosk", duration_ms=200_000)
    store = make_mik_store([{"path": "/gone/x.mp3", "energy": 7.0, "segments": SEGMENTS}])
    assert cli.main(["load", "--json", "--live", "--data-dir", str(lib.library.data_dir), "--store", str(store)]) == 0
    capsys.readouterr()


def test_a_donor_with_the_same_value_but_another_series_is_a_conflict(
    lib: Lib, make_mik_store, write_verdicts, capsys
) -> None:
    """if a donor and a pending analysis agree on the value but not the series and status still says share then broken."""
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    _staged_twins(lib, make_mik_store, ("a", "b", "c"), capsys)
    lib.energy("c", 7)
    lib.series("c")
    assert _without(backfill.status(lib.library, share_across_duplicates=True))["conflicting"] == 0  # control
    lib.conn.execute("UPDATE track_energy_segments SET energy = energy + 1 WHERE stable_id = ?", (sid("c"),))
    lib.conn.commit()
    after = _without(backfill.status(lib.library, share_across_duplicates=True))
    assert after["conflicting"] == 2 and after["would_gain_by_duplicate_sharing"] == 0


def test_a_staged_series_from_an_older_analysis_is_not_a_complete_pending_analysis(
    lib: Lib, make_mik_store, write_verdicts, capsys
) -> None:
    """if a staged scalar is paired with a staged series of another analysis time then broken (rows leave the plan forever)."""
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    _staged_twins(lib, make_mik_store, ("a", "b"), capsys)
    assert _without(backfill.status(lib.library, share_across_duplicates=True))["would_gain_by_loading_shared_analysis"] == 2
    lib.conn.execute(
        "UPDATE unmatched_source_analysis SET modified_at = '2025-01-01T00:00:00+00:00' "
        "WHERE source = 'mik' AND field_name = 'energy_segments'"
    )
    lib.conn.commit()
    after = _without(backfill.status(lib.library, share_across_duplicates=True))
    assert after["would_gain_by_loading_shared_analysis"] == 0
    assert after["needs_fresh_analysis"] == 2


def test_live_share_holds_a_row_a_pending_analysis_disagrees_with(
    lib: Lib, make_mik_store, write_verdicts, capsys
) -> None:
    """if `backfill share` writes a donor's value over a pending staged analysis that disagrees then broken."""
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    _staged_twins(lib, make_mik_store, ("a", "b", "c"), capsys)  # staged 7, ambiguous over a/b/c
    lib.energy("c", 4)
    lib.series("c")  # a library donor that DISAGREES with the pending analysis
    payload = backfill_share.share(lib.library, live=True, share_across_duplicates=True)
    assert payload["counts"]["held_for_pending_analysis"] == 2
    assert payload["counts"]["rows_to_fill"] == 0 and payload["applied"]["fields_written"] == 0
    assert _mik_energy(lib, "a") is None and _mik_energy(lib, "b") is None
    lib.conn.execute("UPDATE track_fields SET value_json = '7' WHERE stable_id = ? AND field_name = 'energy'", (sid("c"),))
    lib.conn.commit()  # control: the donor now agrees, so sharing goes ahead
    agree = backfill_share.share(lib.library, share_across_duplicates=True)
    assert agree["counts"]["held_for_pending_analysis"] == 0 and agree["counts"]["rows_to_fill"] == 2


def test_live_share_holds_a_row_an_unloaded_store_analyzes_directly(lib: Lib, make_mik_store, write_verdicts) -> None:
    """if `share --store` fills a row from a donor while the pending store holds that row's own analysis then broken."""
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    _same_file(lib, "a", "b")
    lib.energy("a", 7)
    lib.series("a")
    store = make_mik_store([{"path": "/Users/old/b/x.mp3", "energy": 4.0, "segments": SEGMENTS}])
    held = backfill_share.share(lib.library, live=True, store=store)
    assert held["counts"]["held_for_pending_analysis"] == 1 and held["applied"]["fields_written"] == 0
    assert _mik_energy(lib, "b") is None
    plain = backfill_share.share(lib.library)  # control: with no pending store the donor fills b
    assert plain["counts"]["held_for_pending_analysis"] == 0 and plain["counts"]["rows_to_fill"] == 1


def test_live_share_holds_a_row_a_scalar_only_store_song_binds(lib: Lib, make_mik_store, write_verdicts) -> None:
    """if `share --store` fills a row whose pending store song has a value but no series then broken (its value is lost)."""
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    _same_file(lib, "a", "b")
    lib.energy("a", 7)
    lib.series("a")
    store = make_mik_store([{"path": "/Users/old/b/x.mp3", "energy": 4.0}])  # scalar only, no segments
    held = backfill_share.share(lib.library, live=True, store=store)
    assert held["counts"]["held_for_pending_analysis"] == 1 and held["applied"]["fields_written"] == 0
    assert _mik_energy(lib, "b") is None
    plan = backfill.plan(lib.library, store=store, ingest_root=lib.ingest)  # status agrees: analyze it fresh
    assert _without(plan)["needs_fresh_analysis"] == 1 and _without(plan)["would_gain_by_duplicate_sharing"] == 0


def test_live_share_holds_rows_an_ambiguous_scalar_only_store_song_reaches(
    lib: Lib, make_mik_store, write_verdicts
) -> None:
    """if an ambiguous value-only pending store song is ignored and a donor fills its candidate then broken."""
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    for letter, folder in (("a", "one"), ("b", "two")):
        lib.track(letter, f"{folder}/x.mp3", title="Tamborel", artist="Quilla Ventrosk", duration_ms=200_000)
    lib.energy("a", 7)
    lib.series("a")
    store = make_mik_store([{"path": "/gone/x.mp3", "energy": 4.0}])  # ambiguous over a/b, no series
    held = backfill_share.share(lib.library, live=True, share_across_duplicates=True, store=store)
    assert held["counts"]["held_for_pending_analysis"] == 1 and held["applied"]["fields_written"] == 0
    plain = backfill_share.share(lib.library, share_across_duplicates=True)  # control: no store, b is filled
    assert plain["counts"]["held_for_pending_analysis"] == 0 and plain["counts"]["rows_to_fill"] == 1


def _load_live(lib: Lib, store, capsys, *extra: str) -> None:
    from apps.mik import cli

    args = ["load", "--json", "--live", "--data-dir", str(lib.library.data_dir), "--store", str(store), *extra]
    assert cli.main(args) == 0
    capsys.readouterr()


def test_a_staged_collision_loser_still_blocks_sharing_after_the_load(
    lib: Lib, make_mik_store, write_verdicts, capsys
) -> None:
    """if a staged lost_collision is forgotten after loading and the winner is shared to the duplicate then broken."""
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    _same_file(lib, "a", "b")
    store = make_mik_store(
        [
            {"path": "/Users/old/a/x.mp3", "energy": 7.0, "segments": SEGMENTS},
            {"path": "/Users/old/a/x.mp3", "energy": 4.0, "segments": SEGMENTS},
        ]
    )
    _load_live(lib, store, capsys)
    assert _mik_energy(lib, "a") is not None and _mik_energy(lib, "b") is None  # winner on a, b left empty
    payload = backfill_share.share(lib.library, live=True)
    assert payload["counts"]["held_for_pending_analysis"] == 1 and payload["applied"]["fields_written"] == 0
    assert _mik_energy(lib, "b") is None


def test_a_staged_value_without_a_series_blocks_donor_sharing(
    lib: Lib, make_mik_store, write_verdicts, capsys
) -> None:
    """if a staged value-only analysis is ignored and a disagreeing donor fills its candidate then broken."""
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    for letter, folder in (("a", "one"), ("b", "two")):
        lib.track(letter, f"{folder}/x.mp3", title="Tamborel", artist="Quilla Ventrosk", duration_ms=200_000)
    _load_live(lib, make_mik_store([{"path": "/gone/x.mp3", "energy": 4.0}]), capsys)  # staged, no series
    lib.energy("a", 7)
    lib.series("a")
    payload = backfill_share.share(lib.library, live=True, share_across_duplicates=True)
    assert payload["counts"]["held_for_pending_analysis"] == 1 and payload["applied"]["fields_written"] == 0
    assert _without(backfill.status(lib.library, share_across_duplicates=True))["needs_fresh_analysis"] == 1


def test_a_target_that_stops_being_a_duplicate_before_the_lock_is_not_filled(lib: Lib, write_verdicts) -> None:
    """if share fills a row a relink committed between planning and the write lock removed from the group then broken."""
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    _same_file(lib, "a", "b")
    lib.energy("a", 7)
    lib.series("a")
    lib.conn.commit()
    holder = sqlite3.connect(lib.library.state_db, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")  # a real writer: share plans against the committed state, then waits for this lock
    holder.execute("DELETE FROM track_availability WHERE stable_id = ?", (sid("b"),))
    release = threading.Timer(1.5, holder.commit)
    release.start()
    try:
        payload = backfill_share.share(lib.library, live=True)
    finally:
        release.join()
        holder.close()
    assert payload["counts"]["rows_to_fill"] == 1  # control: planning really saw b as a duplicate
    assert payload["stale_plan"] is True and _mik_energy(lib, "b") is None


def test_a_duplicate_whose_series_is_gated_off_receives_neither_field(lib: Lib, write_verdicts) -> None:
    """if share writes a duplicate's scalar while its series cannot be written (half a fill, never retried) then broken."""
    write_verdicts({**{field: "passed" for field in loader.GATED_FIELDS}, "energy_segments": "failed"})
    _same_file(lib, "a", "b")
    lib.energy("a", 7)
    lib.series("a")
    payload = backfill_share.share(lib.library, live=True)
    assert payload["counts"]["rows_to_fill"] == 1  # control: b was a planned fill
    assert payload["applied"]["fields_written"] == 0 and _mik_energy(lib, "b") is None


def test_an_unrelated_store_never_hides_a_staged_contradiction(lib: Lib, make_mik_store, write_verdicts, capsys) -> None:
    """if passing an unrelated `--store` drops staged evidence and the donor fills over a staged disagreement then broken."""
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    _staged_twins(lib, make_mik_store, ("a", "b", "c"), capsys)  # staged 7, ambiguous over a/b/c
    lib.energy("c", 4)
    lib.series("c")  # a library donor that DISAGREES with the staged analysis
    unrelated = make_mik_store([{"path": "/elsewhere/unrelated.mp3", "energy": 5.0, "segments": SEGMENTS}], name="other.mikdb")
    payload = backfill_share.share(lib.library, live=True, share_across_duplicates=True, store=unrelated)
    assert payload["counts"]["held_for_pending_analysis"] == 2 and payload["applied"]["fields_written"] == 0
    assert _mik_energy(lib, "a") is None and _mik_energy(lib, "b") is None


def test_a_duplicate_with_only_another_apps_series_is_still_filled(lib: Lib, write_verdicts) -> None:
    """if a duplicate holding only a non-MIK series is reported fillable yet live share writes nothing (never converges) then broken."""
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    _same_file(lib, "a", "b")
    lib.energy("a", 7)
    lib.series("a")
    lib.series("b", source="rekordbox")  # another app's series is no MIK analysis of b
    payload = backfill_share.share(lib.library, live=True)
    assert payload["counts"]["rows_to_fill"] == 1
    assert payload["applied"]["fields_written"] == 1 and _mik_energy(lib, "b") == ("7", "mik")


def test_a_staged_no_candidate_row_that_now_matches_is_held_not_filled(
    lib: Lib, make_mik_store, write_verdicts, capsys
) -> None:
    """if a staged no_candidate analysis that a relink made matchable is ignored and a donor fills its row then broken."""
    from apps.mik import cli

    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    lib.track("a", "Music/x.mp3", stored="/Users/old/a/x.mp3")
    lib.energy("a", 7)
    lib.series("a")
    store = make_mik_store([{"path": "/Users/old/b/y.mp3", "energy": 4.0, "segments": SEGMENTS}])
    assert cli.main(["load", "--json", "--live", "--data-dir", str(lib.library.data_dir), "--store", str(store)]) == 0
    capsys.readouterr()
    reason = lib.conn.execute("SELECT unmatched_reason FROM unmatched_source_analysis WHERE field_name = 'energy'").fetchone()
    assert reason[0] == "no_candidate"  # control: staged with no row to land on
    lib.track("b", "Music/x.mp3", stored="/Users/old/b/y.mp3")  # a relink: b is a's file and the staged song's path
    lib.conn.commit()
    payload = backfill_share.share(lib.library, live=True)
    assert payload["applied"]["fields_written"] == 0 and _mik_energy(lib, "b") is None


def test_a_staged_no_candidate_row_is_rematched_by_exact_path_only(
    lib: Lib, make_mik_store, write_verdicts, capsys
) -> None:
    """if a staged no_candidate row (e.g. a collect batch copy) is fuzzily re-matched to an unrelated row and blocks its fill then broken."""
    from apps.mik import cli

    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    lib.track("a", "Music/x.mp3", stored="/Users/old/a/x.mp3")
    lib.energy("a", 7)
    lib.series("a")
    store = make_mik_store(
        [{"path": "/ingest/mik-batch-1/z.mp3", "name": "Tamborel", "artist": "Quilla Ventrosk", "energy": 4.0, "segments": SEGMENTS}]
    )
    assert cli.main(["load", "--json", "--live", "--data-dir", str(lib.library.data_dir), "--store", str(store)]) == 0
    capsys.readouterr()
    lib.track("c", "Music/x.mp3", stored="/Users/old/c/w.mp3", title="Tamborel", artist="Quilla Ventrosk")
    lib.conn.commit()  # c only shares a title/artist with the staged copy, never its path
    payload = backfill_share.share(lib.library, live=True)
    assert payload["applied"]["fields_written"] == 1 and _mik_energy(lib, "c") == ("7", "mik")
