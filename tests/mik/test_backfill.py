"""Energy backfill: status, plan, stage, collect (MIKBF-03).

- [if] a present row has no MIK energy and nothing can supply it [then] plan lists its file once per recording, [else stop].
- [if] stage runs without --live [then] nothing is copied, and with --live the originals are untouched, [else stop].
- [if] free disk would drop below the floor [then] stage refuses with an explicit error, [else stop].
- [if] collect --live --delete-copies runs [then] only copies whose energy was loaded are removed, [else stop].
"""
from __future__ import annotations

import errno
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.mik import backfill, cli
from apps.mik import backfill_settings as settings
from apps.mik import load as loader
from apps.mik import sources_match as sources
from apps.shared.equivalence_types import EquivalenceGateError
from apps.shared.state.locations import upsert_location
from tests.mik.backfill_support import SEGMENTS, Lib
from tests.mik.backfill_support import sid as _sid

pytestmark = pytest.mark.requirement("MIKBF-03")

NOW = datetime(2026, 10, 13, 12, 34, tzinfo=UTC)


def _without(payload: dict) -> dict:
    return payload["without_mik_energy"]


# ------------------------------------------------------------ status + plan


def test_status_buckets_sum_to_the_present_denominator(lib: Lib) -> None:
    lib.track("a", "Music/a.mp3")
    lib.track("b", "Music/b.mp3")
    lib.track("c", "Music/c.mp3")
    lib.track("d", "Music/d.mp3", state="absent", on_disk=False)
    lib.energy("a", 7)
    lib.series("a")
    lib.energy("b", 4, source="manual")
    payload = backfill.status(lib.library)
    assert payload["denominator"] == "present"
    assert payload["equivalence"] == {"energy": "untested", "energy_segments": "untested"}
    assert payload["present"] == 3  # the absent row is not in the denominator
    assert payload["with_mik_energy"] == 1
    assert payload["energy_from_other_source"] == {"manual": 1}
    assert _without(payload) == {
        "would_gain_by_unique_match": 0,
        "would_gain_by_duplicate_sharing": 0,
        "would_gain_by_loading_shared_analysis": 0,
        "conflicting": 0,
        "needs_series": 0,
        "needs_fresh_analysis": 1,
    }


def test_plan_leaves_out_rows_a_pending_store_would_settle(lib: Lib, make_mik_store) -> None:
    lib.track("a", "Music/a.mp3")
    lib.track("b", "Music/b.mp3")
    store = make_mik_store([{"path": "/Users/old/Music/a.mp3", "energy": 7.0, "segments": SEGMENTS}])
    payload = backfill.plan(lib.library, store=store, ingest_root=lib.ingest)
    assert _without(payload)["would_gain_by_unique_match"] == 1
    assert _without(payload)["needs_fresh_analysis"] == 1
    assert payload["to_analyze"]["files"] == 1
    assert [entry["stable_ids"] for entry in payload["next"]] == [[_sid("b")]]
    assert payload["next"][0]["source"] == str(lib.root / "Music/b.mp3")


def test_a_scalar_only_store_song_does_not_settle_its_row(lib: Lib, make_mik_store) -> None:
    """if a pending store song with an energy but no series counts as settling its row then broken (its series is never analyzed)."""
    lib.track("a", "Music/a.mp3")
    lib.track("b", "Music/b.mp3")
    store = make_mik_store(
        [
            {"path": "/Users/old/Music/a.mp3", "energy": 7.0, "segments": SEGMENTS},  # control: complete
            {"path": "/Users/old/Music/b.mp3", "energy": 4.0},  # scalar only
        ]
    )
    payload = backfill.plan(lib.library, store=store, ingest_root=lib.ingest)
    assert _without(payload)["would_gain_by_unique_match"] == 1
    assert [entry["stable_ids"] for entry in payload["next"]] == [[_sid("b")]]


def test_plan_lists_duplicates_once_only_with_the_flag(lib: Lib) -> None:
    lib.track("a", "one/x.mp3", title="Tamborel", artist="Quilla Ventrosk", duration_ms=200_000)
    lib.track("b", "two/x.mp3", title="Tamborel", artist="Quilla Ventrosk", duration_ms=200_300)
    plain = backfill.plan(lib.library, ingest_root=lib.ingest)
    assert plain["to_analyze"] == {
        "files": 2,
        "rows": 2,
        "held_back": {"already_in_an_open_batch": 0, "no_checked_path": 0},
    }
    grouped = backfill.plan(lib.library, share_across_duplicates=True, ingest_root=lib.ingest)
    assert grouped["to_analyze"]["files"] == 1
    assert grouped["to_analyze"]["rows"] == 2
    assert grouped["next"][0]["stable_ids"] == [_sid("a"), _sid("b")]


def test_status_counts_sharing_and_conflicts_from_staged_rows(
    lib: Lib, make_mik_store, write_verdicts, capsys
) -> None:
    """After a load, the library alone says what sharing would add and what conflicts."""
    for letter, folder in (("a", "one"), ("b", "two")):
        lib.track(letter, f"{folder}/x.mp3", title="Tamborel", artist="Quilla Ventrosk", duration_ms=200_000)
    for letter, folder in (("c", "three"), ("d", "four")):
        lib.track(letter, f"{folder}/y.mp3", title="Pedro", artist="Ruben", duration_ms=300_000)
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    store = make_mik_store(
        [
            {"path": "/gone/x.mp3", "energy": 7.0, "segments": SEGMENTS},
            {"path": "/gone/y.mp3", "energy": 3.0, "segments": SEGMENTS},
            {"path": "/else/y.mp3", "energy": 8.0, "segments": SEGMENTS},
        ]
    )
    argv = ["load", "--json", "--live", "--data-dir", str(lib.library.data_dir), "--store", str(store)]
    assert cli.main(argv) == 0
    capsys.readouterr()
    plain = backfill.status(lib.library)
    assert plain["staged_unmatched"] == {"ambiguous_candidates": 3}
    assert _without(plain)["needs_fresh_analysis"] == 4
    shared = backfill.status(lib.library, share_across_duplicates=True)
    assert _without(shared) == {
        "would_gain_by_unique_match": 0,
        "would_gain_by_duplicate_sharing": 0,  # no complete donor in the library: share cannot fill them
        "would_gain_by_loading_shared_analysis": 2,  # a re-load of the store shares the staged analysis
        "conflicting": 2,
        "needs_series": 0,
        "needs_fresh_analysis": 0,
    }
    plan = backfill.plan(lib.library, share_across_duplicates=True, ingest_root=lib.ingest)
    assert [entry["reason"] for entry in plan["next"]] == ["conflicting_energies"]
    assert plan["next"][0]["stable_ids"] == [_sid("c"), _sid("d")]


def test_plan_rejects_a_limit_outside_the_configured_range(lib: Lib) -> None:
    with pytest.raises(backfill.BackfillError, match="limit"):
        backfill.plan(lib.library, limit=0, ingest_root=lib.ingest)


# ------------------------------------------------------------------- stage


def test_stage_dry_run_copies_nothing(lib: Lib) -> None:
    lib.track("a", "Music/a.mp3")
    payload = backfill.stage(lib.library, ingest_root=lib.ingest, now=NOW)
    assert payload["mode"] == "dry-run"
    assert payload["copied"] is False
    assert payload["counts"]["staged"] == 1
    assert payload["batch_dir"] == str(lib.ingest / "mik-batch-20261013-1234")
    assert not lib.ingest.exists()


def test_stage_live_copies_and_never_touches_the_originals(lib: Lib) -> None:
    original = lib.track("a", "Music/a.mp3")
    lib.track("b", "Music/deep/b.mp3")
    before = (original.read_bytes(), original.stat().st_mtime_ns)
    payload = backfill.stage(lib.library, live=True, ingest_root=lib.ingest, now=NOW)
    batch = Path(payload["batch_dir"])
    assert sorted(path.name for path in batch.iterdir()) == [".batch.json", ".sources.txt", "a.mp3", "b.mp3"]
    assert (batch / "a.mp3").read_bytes() == b"audio a"
    assert (original.read_bytes(), original.stat().st_mtime_ns) == before
    assert sources.read_source_lines(batch) == [str(original), str(lib.root / "Music/deep/b.mp3")]
    assert json.loads((batch / ".batch.json").read_text())["library_root"] == str(lib.root)
    assert payload["library_root"] == str(lib.root)
    assert payload["open_argv"][:3] == ["open", "-a", "Mixed In Key 11"]
    assert payload["open_argv"][3:] == [str(batch / "a.mp3"), str(batch / "b.mp3")]
    assert payload["open_command"].startswith("open -a 'Mixed In Key 11' ")


def test_stage_skips_files_already_in_an_open_batch(lib: Lib) -> None:
    lib.track("a", "Music/a.mp3")
    lib.track("b", "Music/b.mp3")
    first = backfill.stage(lib.library, limit=1, live=True, ingest_root=lib.ingest, now=NOW)
    assert [Path(item["copy"]).name for item in first["files"]] == ["a.mp3"]
    second = backfill.stage(lib.library, limit=5, ingest_root=lib.ingest, now=NOW.replace(minute=35))
    assert [Path(item["copy"]).name for item in second["files"]] == ["b.mp3"]
    assert second["counts"]["held_back"]["already_in_an_open_batch"] == 1


def test_the_disk_floor_is_a_line_drawn_between_three_explicit_numbers(tmp_path: Path) -> None:
    """Both sides of the line, with numbers: nothing here reads a real disk."""
    floor = 10 * 1024**3
    settings.check_disk_floor(free_bytes=floor + 7, needed_bytes=7, min_free_bytes=floor, where=tmp_path)  # exactly at it
    settings.check_disk_floor(free_bytes=2_136_801_273, needed_bytes=7, min_free_bytes=0, where=tmp_path)
    with pytest.raises(backfill.BackfillError, match=r"would leave 10737418239 free .* below the floor of 10737418240"):
        settings.check_disk_floor(free_bytes=floor + 6, needed_bytes=7, min_free_bytes=floor, where=tmp_path)
    with pytest.raises(backfill.BackfillError, match="below the floor"):
        settings.check_disk_floor(free_bytes=2_136_801_273, needed_bytes=7, min_free_bytes=floor, where=tmp_path)


def test_stage_refuses_below_the_configured_floor_and_copies_nothing(lib: Lib) -> None:
    lib.track("a", "Music/a.mp3")
    lib.configure(min_free_bytes=10**18)  # more than any disk has: the library's own setting refuses
    with pytest.raises(backfill.BackfillError, match="below the floor of 1000000000000000000"):
        backfill.stage(lib.library, live=True, now=NOW)
    assert not lib.ingest.exists()
    # an explicit argument on the call outranks the stored setting, in both directions
    assert backfill.stage(lib.library, min_free_bytes=0, now=NOW)["min_free_bytes"] == 0
    lib.configure(min_free_bytes=0)
    with pytest.raises(backfill.BackfillError, match="below the floor"):
        backfill.stage(lib.library, min_free_bytes=10**18, now=NOW)


def test_settings_file_is_validated_and_absent_means_the_defaults(lib: Lib, tmp_path: Path) -> None:
    assert settings.load(lib.library.data_dir) == settings.Settings(ingest_root=lib.ingest, min_free_bytes=0)
    bare = tmp_path / "bare"
    (bare / "state").mkdir(parents=True)
    assert settings.load(bare) == settings.Settings()
    assert settings.Settings().min_free_bytes == 10 * 1024**3
    path = settings.settings_path(bare)
    for bad in ('{"min_free_bytes": -1}', '{"min_free_bytes": "10"}', '{"ingest_root": "relative"}', '{"floor": 1}', "[]", "{"):
        path.write_text(bad, encoding="utf-8")
        with pytest.raises(backfill.BackfillError):
            settings.load(bare)


def test_stage_refuses_an_existing_batch_folder(lib: Lib) -> None:
    lib.track("a", "Music/a.mp3")
    (lib.ingest / "mik-batch-20261013-1234").mkdir(parents=True)
    with pytest.raises(backfill.BackfillError, match="already exists"):
        backfill.stage(lib.library, live=True, ingest_root=lib.ingest, now=NOW)


def test_stage_counts_what_it_holds_back(lib: Lib) -> None:
    lib.track("a", "one/x.mp3")
    lib.track("b", "two/x.mp3")  # same basename: a flat batch holds one of them
    lib.track("c", "Music/c.mp3", on_disk=False)  # recorded present, gone from disk
    payload = backfill.stage(lib.library, ingest_root=lib.ingest, now=NOW)
    assert payload["counts"]["staged"] == 1
    assert payload["counts"]["held_back"]["same_basename"] == 1
    assert payload["counts"]["held_back"]["source_missing"] == 1


# ----------------------------------------------------------------- collect


@pytest.fixture
def staged_batch(lib: Lib, make_mik_store, write_verdicts) -> tuple[Path, Path]:
    """Two files staged live, then 'analyzed': a store naming the COPIES."""
    lib.track("a", "Music/a.mp3")
    lib.track("b", "Music/b.mp3")
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    batch = Path(backfill.stage(lib.library, live=True, ingest_root=lib.ingest, now=NOW)["batch_dir"])
    (batch / "stray.mp3").write_bytes(b"not from the library")
    store = make_mik_store(
        [
            {"path": str(batch / "a.mp3"), "energy": 7.0, "segments": SEGMENTS},
            {"path": str(batch / "b.mp3"), "energy": 3.0, "segments": SEGMENTS},
            {"path": str(batch / "stray.mp3"), "energy": 5.0, "segments": SEGMENTS},
            {"path": "/Users/old/tones/sine.wav", "energy": 1.0},
        ],
        name="Collection11.mikdb",
    )
    return batch, store


def _energy_rows(lib: Lib) -> list[tuple[str, str]]:
    return lib.conn.execute(
        "SELECT stable_id, value_json FROM track_fields "
        "WHERE field_name = 'energy' AND source = 'mik' ORDER BY stable_id"
    ).fetchall()


def test_collect_dry_run_writes_and_deletes_nothing(lib: Lib, staged_batch) -> None:
    batch, store = staged_batch
    payload = backfill.collect(lib.library, backfill.CollectRequest(batch_dir=batch, store=store, delete_copies=True))
    assert payload["mode"] == "dry-run"
    assert payload["sources_batch"]["resolved"] == 2
    assert payload["sources_batch"]["unresolved_by_detail"] == {"no_source_line": 1}
    assert payload["sources_batch"]["ignored_outside_batch"] == 1
    assert payload["planned"]["field_writes"] == 2
    assert payload["copies"] == {"deletable": 2, "deleted": 0, "delete_requested": True}
    assert _energy_rows(lib) == []
    assert (batch / "a.mp3").is_file()


def test_collect_live_loads_energy_and_deletes_only_resolved_copies(lib: Lib, staged_batch) -> None:
    batch, store = staged_batch
    payload = backfill.collect(lib.library, backfill.CollectRequest(batch_dir=batch, store=store, live=True, delete_copies=True))
    assert payload["applied"]["fields_written"] == 2
    assert payload["applied"]["segment_tracks"] == 2
    assert payload["applied"]["staged_written"] == 2  # the stray copy's energy and series
    assert _energy_rows(lib) == [(_sid("a"), "7"), (_sid("b"), "3")]
    assert sorted(path.name for path in batch.iterdir()) == [".batch.json", ".collected.txt", ".sources.txt", "stray.mp3"]
    assert [row["copy"] for row in payload["unresolved"]] == [str(batch / "stray.mp3")]
    assert (lib.root / "Music/a.mp3").read_bytes() == b"audio a"
    after = backfill.status(lib.library)
    assert after["with_mik_energy"] == 2
    assert _without(after)["needs_fresh_analysis"] == 0
    assert backfill.plan(lib.library, ingest_root=lib.ingest)["to_analyze"]["files"] == 0


def test_collect_without_delete_keeps_every_copy(lib: Lib, staged_batch) -> None:
    batch, store = staged_batch
    payload = backfill.collect(lib.library, backfill.CollectRequest(batch_dir=batch, store=store, live=True))
    assert payload["copies"] == {"deletable": 2, "deleted": 0, "delete_requested": False}
    assert backfill.status(lib.library)["equivalence"] == {"energy": "passed", "energy_segments": "passed"}
    assert (batch / "a.mp3").is_file() and (batch / "b.mp3").is_file()


def test_collect_blocked_by_the_gate_writes_and_deletes_nothing(lib: Lib, staged_batch) -> None:
    batch, store = staged_batch
    (lib.library.data_dir / "state" / "equivalence-verdicts.json").unlink()
    payload = backfill.collect(lib.library, backfill.CollectRequest(batch_dir=batch, store=store, live=True, delete_copies=True))
    assert payload["blocked_by_gate"] == {"energy": 3, "energy_segments": 3}
    assert payload["applied"]["fields_written"] == 0
    assert payload["copies"]["deleted"] == 0
    assert _energy_rows(lib) == []
    assert (batch / "a.mp3").is_file()


def test_collect_shares_a_fresh_analysis_with_its_duplicate(lib: Lib, make_mik_store, write_verdicts) -> None:
    lib.track("a", "one/x.mp3", title="Tamborel", artist="Quilla Ventrosk", duration_ms=200_000)
    lib.track("b", "two/x.mp3", title="Tamborel", artist="Quilla Ventrosk", duration_ms=200_300)
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    staged = backfill.stage(
        lib.library, live=True, share_across_duplicates=True, ingest_root=lib.ingest, now=NOW
    )
    assert staged["counts"] == {**staged["counts"], "staged": 1, "rows_settled": 2}
    batch = Path(staged["batch_dir"])
    store = make_mik_store([{"path": str(batch / "x.mp3"), "energy": 6.0, "segments": SEGMENTS}])
    payload = backfill.collect(lib.library, backfill.CollectRequest(batch_dir=batch, store=store, live=True, share_across_duplicates=True))
    assert payload["duplicate_sharing"]["rows_shared"] == 1
    assert _energy_rows(lib) == [(_sid("a"), "6"), (_sid("b"), "6")]


def test_one_file_behind_several_rows_is_analyzed_once_and_reaches_them_all(
    lib: Lib, make_mik_store, write_verdicts
) -> None:
    """Several rows resolving to one file on disk are one recording, with no flag."""
    lib.track("a", "Music/a.mp3")
    lib.track("b", "Music/a.mp3", stored="/Users/user/Recovered/elsewhere/a.mp3")
    lib.track("c", "Music/c.mp3")
    lib.track("d", "Music/c.mp3", stored="/Users/user/Music/c.mp3")  # same tail: two rows equal the line
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    plan = backfill.plan(lib.library, ingest_root=lib.ingest)
    assert plan["to_analyze"]["files"] == 2 and plan["to_analyze"]["rows"] == 4
    assert plan["evidence"]["duplicate_groups_by_basis"] == {"same_file": 2}
    batch = Path(backfill.stage(lib.library, live=True, ingest_root=lib.ingest, now=NOW)["batch_dir"])
    assert sorted(path.name for path in batch.glob("*.mp3")) == ["a.mp3", "c.mp3"]
    store = make_mik_store(
        [
            {"path": str(batch / "a.mp3"), "energy": 7.0, "segments": SEGMENTS},
            {"path": str(batch / "c.mp3"), "energy": 2.0, "segments": SEGMENTS},
        ]
    )
    request = backfill.CollectRequest(batch_dir=batch, store=store, live=True, delete_copies=True)
    payload = backfill.collect(lib.library, request)
    assert payload["sources_batch"]["resolved"] == 1
    assert payload["sources_batch"]["unresolved_by_detail"] == {"several_library_rows": 1}
    assert payload["duplicate_sharing"]["rows_shared_by_basis"] == {"same_file": 3}
    assert payload["unresolved"] == []
    assert _energy_rows(lib) == [(_sid("a"), "7"), (_sid("b"), "7"), (_sid("c"), "2"), (_sid("d"), "2")]
    assert payload["applied"]["staged_written"] == 0
    assert payload["copies"]["deleted"] == 2


@pytest.mark.requirement("MIKBF-09")
def test_a_present_row_sharing_its_stored_path_with_an_absent_row_gets_the_analysis(
    lib: Lib, make_mik_store, write_verdicts
) -> None:
    """if a batch song bound to a present row and an absent row of one stored path reaches neither then broken."""
    lib.track("a", "Music/s.mp3")
    lib.track("b", "Music/s.mp3", state="absent", stored="/Users/old/Music/s.mp3")  # a's own stored path
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    batch = Path(backfill.stage(lib.library, live=True, ingest_root=lib.ingest, now=NOW)["batch_dir"])
    store = make_mik_store([{"path": str(batch / "s.mp3"), "energy": 6.0, "segments": SEGMENTS}])
    payload = backfill.collect(lib.library, backfill.CollectRequest(batch_dir=batch, store=store, live=True))
    assert payload["sources_batch"]["unresolved_by_detail"] == {"several_library_rows": 1}
    assert payload["duplicate_sharing"]["rows_shared_by_basis"] == {"same_stored_path": 2}
    assert payload["unresolved"] == []
    assert _energy_rows(lib) == [(_sid("a"), "6"), (_sid("b"), "6")]


def test_stage_holds_back_a_file_no_row_can_be_bound_to(lib: Lib) -> None:
    lib.track("a", "Music/a.mp3", stored="/Users/user/Recovered/elsewhere/other-name.mp3")
    payload = backfill.stage(lib.library, ingest_root=lib.ingest, now=NOW)
    assert payload["counts"]["staged"] == 0
    assert payload["counts"]["held_back"]["unmappable_source"] == 1


def test_collect_can_read_a_copy_of_the_store_it_is_given(lib: Lib, staged_batch) -> None:
    batch, store = staged_batch
    before = store.stat().st_mtime_ns
    payload = backfill.collect(lib.library, backfill.CollectRequest(batch_dir=batch, store=store, copy_store_first=True))
    assert payload["sources_batch"]["resolved"] == 2
    assert store.stat().st_mtime_ns == before
    assert sorted(path.name for path in store.parent.glob("Collection11.mikdb*")) == ["Collection11.mikdb"]


def test_collect_fails_loudly_on_a_missing_batch_or_store(lib: Lib, tmp_path: Path) -> None:
    with pytest.raises(backfill.BackfillError, match="batch folder"):
        backfill.collect(lib.library, backfill.CollectRequest(batch_dir=tmp_path / "nope", store=tmp_path / "x.mikdb"))
    (tmp_path / "batch").mkdir()
    with pytest.raises(backfill.BackfillError, match=r"no \.batch\.json"):
        backfill.collect(lib.library, backfill.CollectRequest(batch_dir=tmp_path / "batch", store=tmp_path / "x.mikdb"))


# --------------------------------------------------------------------- CLI


def test_cli_runs_the_whole_loop(lib: Lib, staged_batch, capsys) -> None:
    batch, store = staged_batch
    base = ["--json", "--data-dir", str(lib.library.data_dir)]
    assert cli.main(["backfill", "status", *base]) == 0
    assert json.loads(capsys.readouterr().out)["present"] == 2
    assert cli.main(["backfill", "plan", *base, "--ingest-root", str(lib.ingest), "--limit", "5"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["to_analyze"]["held_back"]["already_in_an_open_batch"] == 2
    assert cli.main(["backfill", "stage", *base, "--ingest-root", str(lib.ingest)]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["staged"] == 0
    collect = ["backfill", "collect", *base, "--batch", str(batch), "--store", str(store)]
    assert cli.main([*collect, "--live", "--delete-copies"]) == 0
    assert json.loads(capsys.readouterr().out)["copies"]["deleted"] == 2
    assert cli.main(["backfill", "status", *base]) == 0
    assert json.loads(capsys.readouterr().out)["with_mik_energy"] == 2


def test_cli_text_output_names_the_denominator(lib: Lib, capsys) -> None:
    lib.track("a", "Music/a.mp3")
    argv = ["backfill", "plan", "--data-dir", str(lib.library.data_dir), "--ingest-root", str(lib.ingest)]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "1 present rows (denominator: present)" in out
    assert "need a fresh analysis: 1" in out


def test_cli_collect_needs_a_batch_and_a_store(lib: Lib) -> None:
    with pytest.raises(SystemExit, match="--batch"):
        cli.main(["backfill", "collect", "--data-dir", str(lib.library.data_dir)])


def test_every_backfill_step_is_a_dry_run_by_default() -> None:
    parser = cli.build_parser()
    for step in ("status", "plan", "stage", "collect"):
        args = parser.parse_args(["backfill", step, "--json", "--data-dir", "/tmp"])
        assert args.json is True and args.live is False and args.delete_copies is False


def test_an_explicit_relative_ingest_root_is_refused(tmp_path: Path) -> None:
    """if --ingest-root accepts a relative path that a stored setting would refuse then broken."""
    (tmp_path / "state").mkdir()
    with pytest.raises(settings.BackfillError, match="absolute"):
        settings.resolve(tmp_path, ingest_root=Path("relative/ingest"))
    assert settings.resolve(tmp_path, ingest_root=tmp_path / "abs").ingest_root == tmp_path / "abs"


def test_a_copy_is_kept_until_its_energy_series_is_loaded(lib: Lib, make_mik_store, write_verdicts) -> None:
    """if collect deletes a copy whose energy series did not load then broken (the series would be stranded)."""
    lib.track("a", "Music/a.mp3")
    lib.track("b", "Music/b.mp3")
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    batch = Path(backfill.stage(lib.library, live=True, ingest_root=lib.ingest, now=NOW)["batch_dir"])
    store = make_mik_store(
        [
            {"path": str(batch / "a.mp3"), "energy": 7.0, "segments": SEGMENTS},
            {"path": str(batch / "b.mp3"), "energy": 3.0},  # scalar written, no series yet
        ],
        name="Collection11.mikdb",
    )
    payload = backfill.collect(
        lib.library, backfill.CollectRequest(batch_dir=batch, store=store, live=True, delete_copies=True)
    )
    assert payload["applied"]["fields_written"] == 2
    assert payload["copies"]["deleted"] == 1
    assert not (batch / "a.mp3").exists()
    assert (batch / "b.mp3").is_file()


def test_a_store_copy_that_keeps_its_mtime_still_matches_the_verdict(lib: Lib, staged_batch) -> None:
    """if a copy made with preserved metadata is refused, or a fresh-mtime copy is accepted, then broken."""
    batch, store = staged_batch
    from apps.shared.source_fingerprint import fingerprint

    verdict_path = lib.library.data_dir / "state" / "equivalence-verdicts.json"
    fields = json.loads(verdict_path.read_text(encoding="utf-8"))
    document = {"fields": fields, "meta": {"sources": {"mik": fingerprint(store)}}}
    verdict_path.write_text(json.dumps(document), encoding="utf-8")

    kept = backfill.collect(lib.library, backfill.CollectRequest(batch_dir=batch, store=store, copy_store_first=True))
    assert kept["sources_batch"]["resolved"] == 2

    fresh = store.parent / "fresh-copy" / store.name
    fresh.parent.mkdir()
    fresh.write_bytes(store.read_bytes())
    os.utime(fresh, (store.stat().st_mtime + 60, store.stat().st_mtime + 60))
    with pytest.raises(EquivalenceGateError, match="cp -p"):
        backfill.collect(lib.library, backfill.CollectRequest(batch_dir=batch, store=fresh))


def test_names_that_only_differ_by_case_or_unicode_form_share_one_batch_slot(lib: Lib) -> None:
    """if two originals that land on one copy name are staged together then broken (the second overwrites the first)."""
    lib.track("a", "one/Track.mp3")
    lib.track("b", "two/track.mp3")
    lib.track("c", "three/Zolbré.mp3")  # composed
    lib.track("d", "four/Zolbré.mp3")  # decomposed
    lib.track("e", "five/other.mp3")  # a control: a name that collides with nothing
    payload = backfill.stage(lib.library, ingest_root=lib.ingest, now=NOW)
    # The copies land on THIS host's disk, so its own rule decides: a composed
    # and a decomposed name are one file everywhere (NFC), while Track.mp3 and
    # track.mp3 are one file only where the default volume ignores case.
    case_insensitive = sources.HOST_PLATFORM in sources.CASE_INSENSITIVE_PLATFORMS
    assert payload["counts"]["staged"] == (3 if case_insensitive else 4)
    assert payload["counts"]["held_back"]["same_basename"] == (2 if case_insensitive else 1)


def test_the_sources_file_is_written_with_unix_newlines(lib: Lib) -> None:
    """if .sources.txt is written with the host's newline then broken (one batch, different bytes per OS)."""
    lib.track("a", "Music/a.mp3")
    lib.track("b", "Music/b.mp3")
    batch = Path(backfill.stage(lib.library, live=True, ingest_root=lib.ingest, now=NOW)["batch_dir"])
    raw = (batch / sources.SOURCES_FILE).read_bytes()
    assert raw.count(b"\n") == 2 and b"\r" not in raw


def _relocated(lib: Lib, letter: str, *, live: bool) -> None:
    """A row whose legacy path is stale and whose real place is a LOCAL location row."""
    lib.track(letter, f"Music/{letter}.mp3", stored=f"/Users/user/Recovered/elsewhere/other-{letter}.mp3")
    upsert_location(lib.conn, stable_id=_sid(letter), kind="local", file_path=f"/Users/old/Music/{letter}.mp3")
    if not live:
        lib.conn.execute("UPDATE track_locations SET deleted_at = '2026-10-13T00:00:00+00:00' WHERE stable_id = ?", (_sid(letter),))
    lib.conn.commit()


def test_a_row_playable_through_a_live_local_location_is_stageable_and_bindable(lib: Lib) -> None:
    _relocated(lib, "a", live=True)
    payload = backfill.stage(lib.library, ingest_root=lib.ingest, now=NOW)
    assert payload["counts"]["staged"] == 1, payload["counts"]
    assert payload["counts"]["held_back"]["unmappable_source"] == 0
    assert sources.row_index(lib.conn, platform="linux").by_below_home["/Music/a.mp3"] == {_sid("a")}


def test_a_tombstoned_location_does_not_make_a_row_stageable(lib: Lib) -> None:
    """Control: only LIVE local locations count, so the fix cannot overshoot."""
    _relocated(lib, "a", live=False)
    payload = backfill.stage(lib.library, ingest_root=lib.ingest, now=NOW)
    assert payload["counts"]["staged"] == 0
    assert payload["counts"]["held_back"]["unmappable_source"] == 1
    assert "/Music/a.mp3" not in sources.row_index(lib.conn, platform="linux").by_below_home


def test_a_mik_scalar_without_its_series_is_not_complete_and_is_planned_again(lib: Lib) -> None:
    """if a row with a MIK energy but no MIK series counts as done then broken (its series is never fetched)."""
    lib.track("a", "Music/a.mp3")
    lib.track("b", "Music/b.mp3")
    lib.energy("a", 7)
    lib.series("a")
    lib.energy("b", 3)  # the scalar loaded, the series did not
    payload = backfill.status(lib.library)
    assert payload["with_mik_energy"] == 1
    assert _without(payload)["needs_series"] == 1
    assert payload["with_mik_energy"] + sum(_without(payload).values()) == payload["present"]
    planned = backfill.plan(lib.library, ingest_root=lib.ingest)
    assert [entry["stable_ids"] for entry in planned["next"]] == [[_sid("b")]]
    assert planned["next"][0]["reason"] == "mik_series_missing"


def test_a_series_from_another_source_does_not_complete_a_mik_row(lib: Lib) -> None:
    """Control: only MIK's own series completes a MIK row, and a complete one is never planned."""
    lib.track("a", "Music/a.mp3")
    lib.track("b", "Music/b.mp3")
    lib.energy("a", 7)
    lib.series("a")
    lib.energy("b", 3)
    lib.series("b", source="rekordbox")
    payload = backfill.status(lib.library)
    assert payload["with_mik_energy"] == 1 and _without(payload)["needs_series"] == 1
    planned = backfill.plan(lib.library, ingest_root=lib.ingest)
    assert _sid("a") not in {sid for entry in planned["next"] for sid in entry["stable_ids"]}


def test_a_series_from_an_older_analysis_does_not_complete_a_newer_scalar(lib: Lib) -> None:
    """if a stale MIK series left by an older analysis completes a newer MIK scalar then broken (the row drops out of plans)."""
    lib.track("a", "Music/a.mp3")
    lib.track("b", "Music/b.mp3")
    lib.energy("a", 7)
    lib.series("a")  # control: same analysis time as its scalar, so complete
    lib.energy("b", 3)
    lib.series("b", modified_at="2025-06-01T00:00:00+00:00")  # left over from an older analysis
    payload = backfill.status(lib.library)
    assert payload["with_mik_energy"] == 1 and _without(payload)["needs_series"] == 1
    planned = backfill.plan(lib.library, ingest_root=lib.ingest)
    assert [entry["stable_ids"] for entry in planned["next"]] == [[_sid("b")]]
    assert planned["next"][0]["reason"] == "mik_series_missing"

def test_a_kept_copy_waits_in_its_open_batch_and_is_collected_again(lib: Lib, make_mik_store, write_verdicts) -> None:
    """if a scalar-only row is restaged while its copy is still in an open batch, or cannot be collected again, then broken."""
    lib.track("b", "Music/b.mp3")
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    batch = Path(backfill.stage(lib.library, live=True, ingest_root=lib.ingest, now=NOW)["batch_dir"])
    first = make_mik_store([{"path": str(batch / "b.mp3"), "energy": 3.0}], name="Collection11.mikdb")
    backfill.collect(lib.library, backfill.CollectRequest(batch_dir=batch, store=first, live=True, delete_copies=True))
    assert (batch / "b.mp3").is_file()
    assert _without(backfill.status(lib.library))["needs_series"] == 1
    planned = backfill.plan(lib.library, ingest_root=lib.ingest)
    assert planned["to_analyze"]["files"] == 0
    assert planned["to_analyze"]["held_back"]["already_in_an_open_batch"] == 1
    again = make_mik_store(
        [{"path": str(batch / "b.mp3"), "energy": 3.0, "segments": SEGMENTS}], name="Collection11-again.mikdb"
    )
    payload = backfill.collect(
        lib.library, backfill.CollectRequest(batch_dir=batch, store=again, live=True, delete_copies=True)
    )
    assert payload["applied"]["segment_tracks"] == 1 and payload["copies"]["deleted"] == 1
    after = backfill.status(lib.library)
    assert after["with_mik_energy"] == 1 and _without(after)["needs_series"] == 0


def test_the_launch_command_is_macos_only_and_says_so_elsewhere(lib: Lib) -> None:
    """if a non-macOS stage hands out the open -a command as a working next step then broken."""
    lib.track("a", "Music/a.mp3")
    staged = backfill.stage(lib.library, ingest_root=lib.ingest, now=NOW)
    assert staged["launch_platform"] == "darwin"
    entry = backfill.PlanEntry(source="/src/a.mp3", stable_ids=("a",), reason="no_mik_energy", title=None, artist=None)
    copies = [(entry, Path("/src/a.mp3"), Path("/batch/a.mp3"))]
    mac = backfill._open_command(copies, platform="darwin")
    win = backfill._open_command(copies, platform="win32")
    assert mac["open_argv"] == win["open_argv"] == ["open", "-a", "Mixed In Key 11", "/batch/a.mp3"]
    assert mac["host_supported"] is True and win["host_supported"] is False
    assert mac["next_step"].startswith("run open_command yourself")
    assert "'win32'" in win["next_step"] and "Mixed In Key for Mac" in win["next_step"]


def _hold_pending_wal(store: Path) -> sqlite3.Connection:
    """Leave committed changes in the store's -wal, as Mixed In Key does while it is open."""
    writer = sqlite3.connect(store)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    assert writer.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    writer.execute("INSERT INTO ZSONG (ZNAME) VALUES ('pending-in-wal')")
    writer.commit()
    return writer


def test_a_store_with_unmerged_wal_changes_is_refused(lib: Lib, staged_batch) -> None:
    """if collect reads a store whose -wal holds changes the main file's fingerprint never saw then broken."""
    batch, store = staged_batch
    writer = _hold_pending_wal(store)
    try:
        assert Path(f"{store}-wal").stat().st_size > 0
        with pytest.raises(backfill.BackfillError, match="quit Mixed In Key"):
            backfill.collect(lib.library, backfill.CollectRequest(batch_dir=batch, store=store))
    finally:
        writer.close()
    # Control: once the journal is merged (MIK does this when it quits) the same store loads.
    payload = backfill.collect(lib.library, backfill.CollectRequest(batch_dir=batch, store=store))
    assert payload["sources_batch"]["resolved"] == 2


def test_copy_store_first_reads_a_consistent_snapshot(lib: Lib, staged_batch) -> None:
    """if copy_store_first copies the store's files one by one instead of snapshotting them then broken."""
    _batch, store = staged_batch
    writer = _hold_pending_wal(store)
    try:
        # Mixed In Key open and idle: the -wal is merged but still present.
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        assert Path(f"{store}-wal").is_file() and Path(f"{store}-wal").stat().st_size == 0
        expected = writer.execute("SELECT COUNT(*) FROM ZSONG").fetchone()[0]
        with backfill._store_to_read(store, copy_first=True) as readable:
            assert not Path(f"{readable}-wal").exists()
            snapshot = sqlite3.connect(f"file:{readable}?mode=ro", uri=True)
            try:
                assert snapshot.execute("PRAGMA journal_mode").fetchone()[0] != "wal"
                assert snapshot.execute("SELECT COUNT(*) FROM ZSONG").fetchone()[0] == expected
            finally:
                snapshot.close()
    finally:
        writer.close()


def _a_real_mount_point() -> Path:
    """A mount point this machine really has, one level below its parent (macOS or Linux)."""
    for candidate in (Path("/System/Volumes/Data"), Path("/dev"), Path("/proc"), Path("/sys")):
        if os.path.ismount(candidate):
            return candidate
    raise AssertionError("no known mount point on this machine to control the mounted case against")


def test_an_ingest_root_on_an_unmounted_volume_is_refused(tmp_path: Path) -> None:
    """if staging would recreate a missing volume's mount path on the system disk then broken."""
    mounts = tmp_path / "Volumes"
    mounts.mkdir()
    gone = mounts / "Scratch" / "_ingest"
    assert backfill._unmounted_volume(gone, (mounts,)) == mounts / "Scratch"
    (mounts / "Scratch").mkdir()  # a stale plain folder is not a mounted drive
    assert backfill._unmounted_volume(gone, (mounts,)) == mounts / "Scratch"
    mounted = _a_real_mount_point()
    assert backfill._unmounted_volume(mounted / "_ingest", (mounted.parent,)) is None  # a missing subfolder is fine
    assert backfill._unmounted_volume(tmp_path / "plain" / "_ingest", (mounts,)) is None


def test_a_store_written_during_its_snapshot_is_refused(lib: Lib, staged_batch) -> None:
    """if a commit landing between the WAL check and the end of the backup passes unseen then broken."""
    _batch, store = staged_batch
    writer = _hold_pending_wal(store)
    try:
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # MIK open and idle: the check passes
        before = backfill._quiescent_store_state(store)
        backfill.assert_store_unchanged(store, before)  # control: no write, no refusal
        writer.execute("INSERT INTO ZSONG (ZNAME) VALUES ('committed-mid-snapshot')")
        writer.commit()
        with pytest.raises(backfill.BackfillError, match="quit Mixed In Key"):
            backfill.assert_store_unchanged(store, before)
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # merged into the main file instead
        with pytest.raises(backfill.BackfillError, match="changed while it was being snapshotted"):
            backfill.assert_store_unchanged(store, before)
    finally:
        writer.close()


@pytest.mark.skipif(
    sys.platform.startswith("linux"),
    reason="quarantined on Linux (tech-debt #7695): the snapshot change check never trips there while a writer"
    " checkpoints with TRUNCATE after each commit, even given 60 s; cause not yet known",
)
def test_copy_store_first_refuses_a_snapshot_raced_by_a_live_writer(lib: Lib, staged_batch) -> None:
    """if collect snapshots a store a live writer commits to mid-backup and loads it anyway then broken."""
    _batch, store = staged_batch
    stop = threading.Event()

    def keep_committing() -> None:
        writer = sqlite3.connect(store)
        writer.execute("PRAGMA journal_mode=WAL")
        while not stop.is_set():
            writer.execute("INSERT INTO ZSONG (ZNAME) VALUES ('live-write')")
            writer.commit()
            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # keep the -wal empty so only the recheck can see it
        writer.close()

    thread = threading.Thread(target=keep_committing)
    thread.start()
    refused, deadline = False, time.monotonic() + 10
    try:
        while not refused and time.monotonic() < deadline:
            try:
                with backfill._store_to_read(store, copy_first=True) as readable:
                    backfill.read_store(readable)  # as collect does: the recheck brackets the read
            except backfill.BackfillError as error:
                refused = "changed while it was being snapshotted" in str(error)
    finally:
        stop.set()
        thread.join()
    assert refused, "no snapshot was refused in 10 s of concurrent commits"


def test_a_live_collect_closes_the_sources_it_delivered(lib: Lib, make_mik_store, write_verdicts) -> None:
    """if a delivered source stays 'open' forever (a re-ingested original can never be staged again) then broken."""
    lib.track("b", "Music/b.mp3")
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    batch = Path(backfill.stage(lib.library, live=True, ingest_root=lib.ingest, now=NOW)["batch_dir"])
    key = sources.path_key(str(lib.root / "Music/b.mp3"), platform=sources.HOST_PLATFORM)
    assert key in backfill.open_batch_sources(lib.ingest, platform=sources.HOST_PLATFORM)  # control: open
    scalar_only = make_mik_store([{"path": str(batch / "b.mp3"), "energy": 3.0}], name="scalar.mikdb")
    backfill.collect(lib.library, backfill.CollectRequest(batch_dir=batch, store=scalar_only, live=True))
    assert key in backfill.open_batch_sources(lib.ingest, platform=sources.HOST_PLATFORM)  # not delivered yet
    complete = make_mik_store([{"path": str(batch / "b.mp3"), "energy": 3.0, "segments": SEGMENTS}], name="full.mikdb")
    backfill.collect(lib.library, backfill.CollectRequest(batch_dir=batch, store=complete, live=True))
    assert key not in backfill.open_batch_sources(lib.ingest, platform=sources.HOST_PLATFORM)


def test_a_source_written_after_planning_is_not_overwritten(lib: Lib) -> None:
    """if a field another source saved between build_plan and apply_plan is overwritten by MIK then broken."""
    from apps.mik.load_types import FieldWrite, LoadPlan, SegmentWrite
    from apps.mik.mikdb import EnergySegment

    lib.track("a", "Music/a.mp3")
    lib.track("b", "Music/b.mp3")
    series = (EnergySegment(0, 0, 60_000, 5),)
    plan = LoadPlan(
        field_writes=[
            FieldWrite(_sid(letter), "energy", 6, 0.95, "2026-10-20T00:00:00+00:00", "sources_batch")
            for letter in ("a", "b")
        ],
        segment_writes=[
            SegmentWrite(_sid(letter), series, 0.95, "2026-10-20T00:00:00+00:00", "sources_batch")
            for letter in ("a", "b")
        ],
    )
    lib.energy("a", 3, source="rekordbox")  # committed AFTER the plan was built
    result = loader.apply_plan(lib.conn, plan)
    assert result.blocked_at_write == 1 and result.fields_written == 1 and result.segment_tracks == 1
    source_of = dict(lib.conn.execute("SELECT stable_id, source FROM track_fields WHERE field_name = 'energy'"))
    assert source_of == {_sid("a"): "rekordbox", _sid("b"): "mik"}  # control: b, untouched since, is written
    with_series = {row[0] for row in lib.conn.execute("SELECT stable_id FROM track_energy_segments WHERE source = 'mik'")}
    assert with_series == {_sid("b")}


def test_a_mik_value_written_after_planning_blocks_a_fill_planned_against_empty(lib: Lib) -> None:
    """if a write planned against one field state overwrites a newer state of it (any source, MIK included) then broken."""
    from apps.mik.load_plan import _field_versions
    from apps.mik.load_types import FieldWrite, LoadPlan

    for letter in ("a", "b", "c"):
        lib.track(letter, f"Music/{letter}.mp3")
    lib.energy("b", 4, source="mik")
    lib.energy("c", 4, source="mik")
    planned = _field_versions(lib.conn, ["energy"])
    stamp = "2026-10-20T00:00:00+00:00"
    plan = LoadPlan(
        field_writes=[
            FieldWrite(
                _sid(letter), "energy", 6, 0.95, stamp, "exact_path", planned_version=planned.get((_sid(letter), "energy"))
            )
            for letter in ("a", "b", "c")
        ],
    )
    lib.energy("a", 3, source="mik")  # a's own analysis, collected AFTER a fill planned against empty
    lib.energy("c", 5, source="mik")  # a NEWER mik analysis of c: same source, different value
    result = loader.apply_plan(lib.conn, plan)
    assert result.held_rows == frozenset({_sid("a"), _sid("c")})
    value_of = dict(lib.conn.execute("SELECT stable_id, value_json FROM track_fields WHERE field_name = 'energy'"))
    assert value_of == {_sid("a"): "3", _sid("b"): "6", _sid("c"): "5"}  # control: b, unchanged since, is written


def test_a_plan_whose_still_valid_check_fails_under_the_lock_writes_nothing(lib: Lib) -> None:
    """if apply_plan writes a plan whose still_valid re-check fails under the lock then broken."""
    from apps.mik.load_types import FieldWrite, LoadPlan

    lib.track("a", "Music/a.mp3")
    write = FieldWrite(_sid("a"), "energy", 6, 0.95, "2026-10-20T00:00:00+00:00", "shared_duplicate")
    refused = loader.apply_plan(lib.conn, LoadPlan(field_writes=[write], still_valid=lambda _conn: False))
    assert refused.stale_plan and refused.fields_written == 0
    assert lib.conn.execute("SELECT COUNT(*) FROM track_fields").fetchone()[0] == 0
    accepted = loader.apply_plan(lib.conn, LoadPlan(field_writes=[write], still_valid=lambda _conn: True))
    assert not accepted.stale_plan and accepted.fields_written == 1  # control: a passing check writes


@pytest.mark.skipif(
    sys.platform.startswith("linux"),
    reason="quarantined on Linux (tech-debt #7695): the snapshot change check never trips there while a writer"
    " checkpoints with TRUNCATE after each commit, even given 60 s; cause not yet known",
)
def test_a_direct_read_refuses_a_store_written_while_it_was_read(lib: Lib, staged_batch) -> None:
    """if a direct (non-snapshot) read of a store a live writer commits to mid-read is accepted then broken."""
    _batch, store = staged_batch
    stop = threading.Event()

    def keep_committing() -> None:
        writer = sqlite3.connect(store)
        writer.execute("PRAGMA journal_mode=WAL")
        while not stop.is_set():
            writer.execute("INSERT INTO ZSONG (ZNAME) VALUES ('live-write')")
            writer.commit()
            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # keep the -wal empty so only the recheck can see it
        writer.close()

    thread = threading.Thread(target=keep_committing)
    thread.start()
    refused, deadline = False, time.monotonic() + 10
    try:
        while not refused and time.monotonic() < deadline:
            try:
                with backfill._store_to_read(store, copy_first=False) as readable:
                    backfill.read_store(readable)
            except backfill.BackfillError as error:
                refused = "changed while" in str(error)
    finally:
        stop.set()
        thread.join()
    assert refused, "no direct read was refused in 10 s of concurrent commits"


def test_collect_refuses_sharing_to_a_row_deleted_between_planning_and_the_lock(
    lib: Lib, make_mik_store, write_verdicts
) -> None:
    """if collect shares to a duplicate a writer removed after planning, or closes the source anyway, then broken."""
    lib.track("a", "one/x.mp3", title="Tamborel", artist="Quilla Ventrosk", duration_ms=200_000)
    lib.track("b", "two/x.mp3", title="Tamborel", artist="Quilla Ventrosk", duration_ms=200_300)
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    staged = backfill.stage(lib.library, live=True, share_across_duplicates=True, ingest_root=lib.ingest, now=NOW)
    batch = Path(staged["batch_dir"])
    store = make_mik_store([{"path": str(batch / "x.mp3"), "energy": 6.0, "segments": SEGMENTS}])
    lib.conn.commit()
    holder = sqlite3.connect(lib.library.state_db, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")  # a real writer: collect plans against the committed state, then waits
    holder.execute("UPDATE tracks SET deleted_at = '2026-10-20T00:00:00+00:00' WHERE stable_id = ?", (_sid("b"),))
    release = threading.Timer(1.5, holder.commit)
    release.start()
    try:
        payload = backfill.collect(
            lib.library, backfill.CollectRequest(batch_dir=batch, store=store, live=True, share_across_duplicates=True)
        )
    finally:
        release.join()
        holder.close()
    assert payload["duplicate_sharing"]["rows_shared"] == 1  # control: planning really shared to b
    assert payload["applied"]["stale_plan"] is True and _energy_rows(lib) == []
    assert batch.name in {p.name for p in lib.ingest.iterdir()} and payload["copies"]["deletable"] == 0


def test_a_failed_copy_leaves_no_partial_batch_folder(lib: Lib) -> None:
    """if stage leaves a batch folder with no manifest behind when a copy fails (orphaned copies) then broken."""
    if os.geteuid() == 0:
        pytest.skip("capability unavailable: root reads a mode-000 file, so no copy can fail this way")
    lib.track("a", "Music/a.mp3")
    source = lib.track("b", "Music/b.mp3")
    source.chmod(0)
    try:
        with pytest.raises(PermissionError):
            backfill.stage(lib.library, live=True, ingest_root=lib.ingest, now=NOW)
    finally:
        source.chmod(0o644)
    assert not list(lib.ingest.glob("mik-batch-*"))
    assert backfill.stage(lib.library, live=True, ingest_root=lib.ingest, now=NOW)["counts"]["staged"] == 2  # control


def test_an_ingest_root_reached_through_a_symlink_is_judged_by_its_target(tmp_path: Path) -> None:
    """if a symlinked ingest root pointing at an unmounted volume's stale folder is accepted then broken."""
    mounts = tmp_path / "Volumes"
    (mounts / "Scratch").mkdir(parents=True)  # the drive is gone, a stale plain folder remains
    link = tmp_path / "home" / "mik-ingest"
    link.parent.mkdir()
    link.symlink_to(mounts / "Scratch")
    assert backfill._unmounted_volume(link, (mounts,)) == (mounts / "Scratch").resolve()
    plain = tmp_path / "home" / "plain-ingest"
    plain.mkdir()
    assert backfill._unmounted_volume(plain, (mounts,)) is None  # control: a real local folder is fine


def test_a_copy_reaching_a_held_row_is_not_delivered(lib: Lib) -> None:
    """if a copy whose analysis reached a held duplicate is closed or deleted because another row was written then broken."""
    reached, written = {"A", "B"}, {"A"}
    assert backfill._fully_delivered(reached, written, written, frozenset({"B"})) is False
    assert backfill._fully_delivered(reached, written, written, frozenset()) is True  # control: B was not held


def test_collect_refuses_a_direct_match_deleted_between_planning_and_the_lock(lib: Lib, make_mik_store, write_verdicts) -> None:
    """if collect writes a song to the row it matched after a writer deleted that row, or closes its source, then broken."""
    lib.track("a", "Music/a.mp3")
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    batch = Path(backfill.stage(lib.library, live=True, ingest_root=lib.ingest, now=NOW)["batch_dir"])
    store = make_mik_store([{"path": str(batch / "a.mp3"), "energy": 6.0, "segments": SEGMENTS}])
    key = sources.path_key(str(lib.root / "Music/a.mp3"), platform=sources.HOST_PLATFORM)
    lib.conn.commit()
    holder = sqlite3.connect(lib.library.state_db, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")  # a real writer: collect plans against the committed state, then waits
    holder.execute("UPDATE tracks SET deleted_at = '2026-10-20T00:00:00+00:00' WHERE stable_id = ?", (_sid("a"),))
    release = threading.Timer(1.5, holder.commit)
    release.start()
    try:
        payload = backfill.collect(lib.library, backfill.CollectRequest(batch_dir=batch, store=store, live=True))
    finally:
        release.join()
        holder.close()
    assert payload["planned"]["field_writes"] >= 1  # control: planning really bound the song to a
    assert payload["applied"]["stale_plan"] is True and _energy_rows(lib) == []
    assert key in backfill.open_batch_sources(lib.ingest, platform=sources.HOST_PLATFORM)


def test_a_store_replaced_while_its_snapshot_is_in_use_is_refused(lib: Lib, staged_batch, make_mik_store) -> None:
    """if the original store is swapped after snapshotting and the gate trusts the new file's fingerprint then broken."""
    _batch, store = staged_batch
    other = make_mik_store([{"path": "/elsewhere/other.mp3", "energy": 2.0}], name="other.mikdb")
    with pytest.raises(backfill.BackfillError, match="changed while"), backfill._store_to_read(store, copy_first=True):
        shutil.copyfile(other, store)  # a different store restored at the original path mid-read
    with backfill._store_to_read(other, copy_first=True) as readable:  # control: an untouched store reads clean
        assert backfill.read_store(readable)


def test_a_staged_analysis_written_after_planning_is_not_overwritten(lib: Lib) -> None:
    """if an older plan's staged write replaces a newer staged analysis another run committed after planning then broken."""
    from apps.mik.load_plan import staged_versions
    from apps.mik.load_types import LoadPlan, StagedWrite

    def staged(row_id: str, value: int, at: str) -> StagedWrite:
        return StagedWrite(row_id, "energy", value, 0.9, "no_candidate", "T", "A", None, None, "/gone/x.mp3", at)

    keys = {("41", "energy"), ("42", "energy")}
    older = LoadPlan(staged=[staged("41", 3, "2026-10-01T00:00:00+00:00"), staged("42", 3, "2026-10-01T00:00:00+00:00")])
    older.staged_versions = staged_versions(lib.conn, keys)  # planned while both rows were absent
    newer = LoadPlan(staged=[staged("41", 8, "2026-10-20T00:00:00+00:00")])
    newer.staged_versions = staged_versions(lib.conn, {("41", "energy")})
    loader.apply_plan(lib.conn, newer)  # another run commits first
    result = loader.apply_plan(lib.conn, older)
    assert result.staged_held == 1
    value_of = dict(lib.conn.execute("SELECT source_row_id, value_json FROM unmatched_source_analysis WHERE field_name = 'energy'"))
    assert value_of == {"41": "8", "42": "3"}  # control: row 42, untouched since planning, is staged


def test_a_series_replaced_after_planning_is_not_overwritten(lib: Lib) -> None:
    """if a series-only plan replaces a newer series another run stored after planning then broken."""
    from apps.mik.load_plan import series_version
    from apps.mik.load_types import LoadPlan, SegmentWrite
    from apps.mik.mikdb import EnergySegment

    lib.track("a", "Music/a.mp3")
    lib.track("b", "Music/b.mp3")
    old, new = (EnergySegment(0, 0, 60_000, 3),), (EnergySegment(0, 0, 60_000, 9),)
    stale = LoadPlan(segment_writes=[SegmentWrite(_sid(x), old, 0.9, "2026-10-01T00:00:00+00:00", "exact_path") for x in "ab"])
    stale.series_versions = {_sid(x): series_version(lib.conn, _sid(x)) for x in "ab"}  # both empty at planning
    newer = LoadPlan(segment_writes=[SegmentWrite(_sid("a"), new, 0.9, "2026-10-20T00:00:00+00:00", "exact_path")])
    loader.apply_plan(lib.conn, newer)  # another run stores a's newer series first
    result = loader.apply_plan(lib.conn, stale)
    assert result.series_held == 1 and result.segment_tracks == 1  # control: b, untouched since, is written
    energy_of = dict(lib.conn.execute("SELECT stable_id, energy FROM track_energy_segments WHERE source = 'mik'"))
    assert energy_of == {_sid("a"): 9, _sid("b"): 3}


def test_a_staged_row_whose_metadata_changed_after_planning_is_not_overwritten(lib: Lib) -> None:
    """if a stale plan overwrites a newer staged row that differs only in its matching metadata then broken."""
    from apps.mik.load_plan import staged_versions
    from apps.mik.load_types import LoadPlan, StagedWrite

    at = "2026-10-01T00:00:00+00:00"

    def staged(reason: str, path: str) -> StagedWrite:
        return StagedWrite("41", "energy", 5, 0.9, reason, "T", "A", None, None, path, at)

    loader.apply_plan(lib.conn, LoadPlan(staged=[staged("no_candidate", "/gone/a.mp3")]))
    stale = LoadPlan(staged=[staged("no_candidate", "/gone/a.mp3")])
    stale.staged_versions = staged_versions(lib.conn, {("41", "energy")})
    loader.apply_plan(lib.conn, LoadPlan(staged=[staged("ambiguous_candidates", "/elsewhere/a.mp3")]))  # same value and time
    assert loader.apply_plan(lib.conn, stale).staged_held == 1
    row = lib.conn.execute("SELECT unmatched_reason, source_path FROM unmatched_source_analysis WHERE source_row_id = '41'").fetchone()
    assert tuple(row) == ("ambiguous_candidates", "/elsewhere/a.mp3")


def test_collect_never_overwrites_a_duplicate_holding_its_own_analysis(lib: Lib, make_mik_store, write_verdicts) -> None:
    """if a fresh analysis of one copy overwrites a duplicate's own existing MIK analysis instead of only filling empties then broken."""
    lib.track("a", "one/x.mp3", title="Tamborel", artist="Quilla Ventrosk", duration_ms=200_000)
    lib.track("b", "two/x.mp3", title="Tamborel", artist="Quilla Ventrosk", duration_ms=200_300)
    lib.track("c", "three/x.mp3", title="Tamborel", artist="Quilla Ventrosk", duration_ms=200_100)
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    staged = backfill.stage(lib.library, live=True, share_across_duplicates=True, ingest_root=lib.ingest, now=NOW)
    batch = Path(staged["batch_dir"])
    lib.energy("b", 4)
    lib.series("b")  # b already holds its own MIK analysis
    store = make_mik_store([{"path": str(batch / "x.mp3"), "energy": 6.0, "segments": SEGMENTS}])
    backfill.collect(lib.library, backfill.CollectRequest(batch_dir=batch, store=store, live=True, share_across_duplicates=True))
    energy_of = dict(_energy_rows(lib))
    assert energy_of[_sid("b")] == "4"
    assert energy_of[_sid("a")] == "6" and energy_of[_sid("c")] == "6"  # control: the empty duplicates are still filled


def test_a_series_stored_after_planning_holds_the_rows_scalar_too(lib: Lib) -> None:
    """if a fill writes its scalar beside a series another run stored after planning (a mixed pair) or reports it delivered then broken."""
    from apps.mik.load_plan import series_version
    from apps.mik.load_types import FieldWrite, LoadPlan, SegmentWrite
    from apps.mik.mikdb import EnergySegment

    lib.track("a", "Music/a.mp3")
    lib.track("b", "Music/b.mp3")
    at = "2026-10-01T00:00:00+00:00"
    mine = (EnergySegment(0, 0, 60_000, 6),)
    stale = LoadPlan(
        field_writes=[FieldWrite(_sid(x), "energy", 6, 0.9, at, "shared_duplicate") for x in "ab"],
        segment_writes=[SegmentWrite(_sid(x), mine, 0.9, at, "shared_duplicate") for x in "ab"],
    )
    stale.series_versions = {_sid(x): series_version(lib.conn, _sid(x)) for x in "ab"}
    other = (EnergySegment(0, 0, 60_000, 2),)
    loader.apply_plan(lib.conn, LoadPlan(segment_writes=[SegmentWrite(_sid("a"), other, 0.9, at, "exact_path")]))
    result = loader.apply_plan(lib.conn, stale)
    assert result.series_held == 1 and _sid("a") in result.held_rows and _sid("b") not in result.held_rows
    scalars = dict(lib.conn.execute("SELECT stable_id, value_json FROM track_fields WHERE field_name = 'energy'"))
    assert scalars == {_sid("b"): "6"}  # a got neither half; control: b got both


def test_stage_skips_a_source_whose_name_holds_a_line_break(lib: Lib) -> None:
    """if stage writes a path containing a line break into the one-path-per-line sources file then broken."""
    lib.track("a", "Music/a.mp3")
    lib.track("b", "Music/two\nlines.mp3")
    payload = backfill.stage(lib.library, ingest_root=lib.ingest, now=NOW)
    assert payload["counts"]["held_back"]["line_break_in_path"] == 1
    assert payload["counts"]["staged"] == 1  # control: the ordinary file is staged


def test_a_promoted_song_still_fills_its_matchs_duplicates(lib: Lib, write_verdicts) -> None:
    """if a song earlier promoted to track A is barred from filling A's empty duplicate B as a promotion conflict then broken."""
    from apps.mik.match import Match, MatchReport
    from apps.mik.mikdb import EnergySegment, MikSong
    from apps.shared.equivalence import EquivalenceGate

    lib.track("a", "Music/a.mp3")
    lib.track("b", "Music/b.mp3")
    lib.track("c", "Music/c.mp3")
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    for field_name in ("energy", "energy_segments"):
        lib.conn.execute(
            "INSERT INTO unmatched_source_analysis(source, source_row_id, field_name, value_json, unmatched_reason, "
            "modified_at, imported_at, promoted_stable_id) VALUES ('mik', '7', ?, '6', 'no_candidate', ?, ?, ?)",
            (field_name, NOW, NOW, _sid("a")),
        )
    song = MikSong(7, None, None, None, None, None, None, None, 6, None, None, None, NOW.isoformat(), (EnergySegment(0, 0, 60_000, 6),))
    report = MatchReport()
    report.matches[7] = Match(7, _sid("a"), "exact_path")
    gate = EquivalenceGate.load(lib.library.data_dir)
    shared = {7: (Match(7, _sid("b"), "shared_duplicate"),)}
    plan = loader.build_plan([song], report, gate, lib.conn, shared=shared)
    planned = {write.stable_id for write in plan.field_writes if write.field_name == "energy"}
    assert planned == {_sid("a"), _sid("b")}
    report.matches[7] = Match(7, _sid("c"), "exact_path")  # control: a DIRECT match that disagrees with the promotion
    moved = loader.build_plan([song], report, gate, lib.conn)
    assert not [write for write in moved.field_writes if write.field_name == "energy"]


def test_a_misbound_promoted_song_shares_nothing(lib: Lib, write_verdicts) -> None:
    """if a song promoted to A but now matched to C is refused on C yet still fills C's duplicate D then broken."""
    from apps.mik.match import Match, MatchReport
    from apps.mik.mikdb import EnergySegment, MikSong
    from apps.shared.equivalence import EquivalenceGate

    for letter in "acd":
        lib.track(letter, f"Music/{letter}.mp3")
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    for field_name in ("energy", "energy_segments"):
        lib.conn.execute(
            "INSERT INTO unmatched_source_analysis(source, source_row_id, field_name, value_json, unmatched_reason, "
            "modified_at, imported_at, promoted_stable_id) VALUES ('mik', '7', ?, '6', 'no_candidate', ?, ?, ?)",
            (field_name, NOW, NOW, _sid("a")),
        )
    song = MikSong(7, None, None, None, None, None, None, None, 6, None, None, None, NOW.isoformat(), (EnergySegment(0, 0, 60_000, 6),))
    report = MatchReport()
    report.matches[7] = Match(7, _sid("c"), "exact_path")  # disagrees with the promotion to a
    plan = loader.build_plan(
        [song], report, EquivalenceGate.load(lib.library.data_dir), lib.conn,
        shared={7: (Match(7, _sid("d"), "shared_duplicate"),)},
    )
    assert not [write for write in plan.field_writes if write.field_name == "energy"]
    assert plan.blocked_by_promotion_conflict.get("shared_fills") == 1


def test_a_promotion_committed_after_planning_refuses_the_plan(lib: Lib, write_verdicts) -> None:
    """if a source row promoted to A after planning is still written to the C the stale plan matched then broken."""
    from apps.mik.match import Match, MatchReport
    from apps.mik.mikdb import EnergySegment, MikSong
    from apps.shared.equivalence import EquivalenceGate

    for letter in "ac":
        lib.track(letter, f"Music/{letter}.mp3")
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    song = MikSong(7, None, None, None, None, None, None, None, 6, None, None, None, NOW.isoformat(), (EnergySegment(0, 0, 60_000, 6),))
    report = MatchReport()
    report.matches[7] = Match(7, _sid("c"), "exact_path")
    gate = EquivalenceGate.load(lib.library.data_dir)
    plan = loader.build_plan([song], report, gate, lib.conn)
    assert [w for w in plan.field_writes if w.field_name == "energy"]  # control: planned onto c
    lib.conn.execute(
        "INSERT INTO unmatched_source_analysis(source, source_row_id, field_name, value_json, unmatched_reason, "
        "modified_at, imported_at, promoted_stable_id) VALUES ('mik', '7', 'energy', '6', 'no_candidate', ?, ?, ?)",
        (NOW, NOW, _sid("a")),
    )
    lib.conn.commit()  # a concurrent promote binds row 7 to a before the lock
    result = loader.apply_plan(lib.conn, plan)
    assert result.stale_plan and lib.conn.execute("SELECT COUNT(*) FROM track_fields WHERE field_name = 'energy'").fetchone()[0] == 0
    fresh = loader.build_plan([song], report, gate, lib.conn)  # control: an unchanged plan applies
    assert not loader.apply_plan(lib.conn, fresh).stale_plan


def test_a_staged_batch_copy_replays_its_source_map_not_fuzzy_tiers(lib: Lib, tmp_path: Path) -> None:
    """if an ambiguous staged batch copy is bound by basename to an unrelated row instead of its mapped original then broken."""
    from apps.mik import backfill_picture as picture
    from apps.mik.match import TrackIndex

    lib.track("a", "Music/orig.mp3", stored="/Users/old/a/orig.mp3")
    lib.track("u", "Other/z.mp3", stored="/Users/old/u/z.mp3")  # shares only the copy's basename
    lib.conn.commit()
    batch = tmp_path / "mik-batch-20261022-1200"
    batch.mkdir()
    (batch / ".sources.txt").write_text("/Users/old/a/orig.mp3\n", encoding="utf-8")
    (batch / ".batch.json").write_text(json.dumps({"library_root": None}), encoding="utf-8")
    copy = str(batch / "orig.mp3")
    index = TrackIndex.from_conn(lib.conn)
    rows = sources.row_index(lib.conn, platform=sources.HOST_PLATFORM)
    found, _tier = picture._staged_row_candidates(index, rows, "ambiguous_candidates", copy, None, None)
    assert found == [_sid("a")]  # the mapped original, by exact path
    stray = str(batch / "z.mp3")  # a copy the map does not name
    assert picture._staged_row_candidates(index, rows, "ambiguous_candidates", stray, None, None)[0] == []


def test_a_staged_batch_copy_replays_its_batchs_library_root(lib: Lib, tmp_path: Path) -> None:
    """if a staged copy bound below the batch's library root loses that binding on replay, so a donor can fill its row, then broken."""
    from apps.mik import backfill_picture as picture
    from apps.mik.match import TrackIndex

    lib.track("a", "Music/orig.mp3", stored="/Users/old/Music/orig.mp3")  # the row lives under a home
    lib.conn.commit()
    batch = tmp_path / "mik-batch-20261022-1300"
    batch.mkdir()
    (batch / ".sources.txt").write_text("/Volumes/USB/Music/orig.mp3\n", encoding="utf-8")  # read from the drive
    (batch / ".batch.json").write_text(json.dumps({"library_root": "/Volumes/USB"}), encoding="utf-8")
    copy = str(batch / "orig.mp3")
    index = TrackIndex.from_conn(lib.conn)
    rows = sources.row_index(lib.conn, platform=sources.HOST_PLATFORM)
    found, _tier = picture._staged_row_candidates(index, rows, "ambiguous_candidates", copy, None, None)
    assert found == [_sid("a")]  # bound below the root, exactly as collect bound it
    (batch / ".batch.json").write_text(json.dumps({"library_root": None}), encoding="utf-8")
    # control: with no root only the whole path binds, and the drive path is no row's
    assert picture._staged_row_candidates(index, rows, "ambiguous_candidates", copy, None, None)[0] == []


def test_a_copy_a_peer_collect_already_deleted_counts_as_settled(tmp_path: Path) -> None:
    """if a collect fails after committing because a peer collect removed the same delivered copy first then broken."""
    kept = tmp_path / "kept.mp3"
    kept.write_bytes(b"x")
    gone = tmp_path / "gone.mp3"  # a peer already removed it
    assert backfill._delete_copies([kept, gone]) == 2
    assert not kept.exists()  # control: a present copy is still removed


def test_a_duplicate_with_only_a_mik_scalar_gets_the_full_analysis(lib: Lib, make_mik_store, write_verdicts) -> None:
    """if a duplicate holding a MIK value without its series is never completed by a fresh shared analysis then broken."""
    lib.track("a", "one/x.mp3", title="Tamborel", artist="Quilla Ventrosk", duration_ms=200_000)
    lib.track("b", "two/x.mp3", title="Tamborel", artist="Quilla Ventrosk", duration_ms=200_300)
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    staged = backfill.stage(lib.library, live=True, share_across_duplicates=True, ingest_root=lib.ingest, now=NOW)
    batch = Path(staged["batch_dir"])
    lib.energy("b", 4)  # a MIK value with no series: needs_series
    store = make_mik_store([{"path": str(batch / "x.mp3"), "energy": 6.0, "segments": SEGMENTS}])
    backfill.collect(lib.library, backfill.CollectRequest(batch_dir=batch, store=store, live=True, share_across_duplicates=True))
    with_series = {row[0] for row in lib.conn.execute("SELECT DISTINCT stable_id FROM track_energy_segments WHERE source = 'mik'")}
    assert _sid("b") in with_series and dict(_energy_rows(lib))[_sid("b")] == "6"


def test_a_live_stage_waits_for_another_live_stage(lib: Lib) -> None:
    """if two live stages of one library run at once (same files in two batches, or a 500 on one minute's folder) then broken."""
    lib.track("a", "Music/a.mp3")
    done = threading.Event()

    def stage_in_background() -> None:
        backfill.stage(lib.library, live=True, ingest_root=lib.ingest, now=NOW)
        done.set()

    with backfill._stage_lock(lib.library.data_dir):  # another stage holds the library
        thread = threading.Thread(target=stage_in_background)
        thread.start()
        assert not done.wait(1.0)  # still waiting on the lock
    thread.join(timeout=30)
    assert done.is_set()  # control: it proceeds once the lock is released
    with pytest.raises(backfill.BackfillError, match="already exists"):  # same minute: a clear error, not FileExistsError
        backfill.stage(lib.library, live=True, ingest_root=lib.ingest, now=NOW)


class _FakeMsvcrt:
    """Stands in for msvcrt: busy for ``busy_rounds`` LK_LOCK calls, then raises ``then`` or succeeds."""

    LK_LOCK = 1

    def __init__(self, busy_rounds: int, then: OSError | None = None) -> None:
        self.calls: list[tuple[int, int, int]] = []
        self.busy_rounds, self.then = busy_rounds, then

    def locking(self, descriptor: int, mode: int, nbytes: int) -> None:
        self.calls.append((os.lseek(descriptor, 0, os.SEEK_CUR), mode, nbytes))
        if len(self.calls) > 10:  # a retry loop that never stops fails here instead of hanging
            raise AssertionError("stage lock kept retrying past 10 calls")
        if len(self.calls) <= self.busy_rounds:
            raise OSError(errno.EDEADLK, "Resource deadlock avoided")
        if self.then is not None:
            raise self.then


def _windows_stage_lock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake: _FakeMsvcrt) -> None:
    with monkeypatch.context() as patch:  # undone before a failure report builds paths
        patch.setattr(os, "name", "nt")
        patch.setitem(sys.modules, "msvcrt", fake)
        patch.setattr(backfill.time, "sleep", lambda _s: None)
        with backfill._stage_lock(tmp_path):
            pass


def test_the_windows_stage_lock_keeps_waiting_past_lk_locks_retries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """if the real Windows stage lock gives up while a peer holds it past LK_LOCK's ten tries then broken."""
    fake = _FakeMsvcrt(busy_rounds=3)
    _windows_stage_lock(monkeypatch, tmp_path, fake)
    assert fake.calls == [(0, fake.LK_LOCK, 1)] * 4  # control: stops once taken, always byte 0


def test_the_windows_stage_lock_raises_a_non_contention_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """if the Windows stage lock retries a filesystem error forever instead of raising it then broken."""
    fake = _FakeMsvcrt(busy_rounds=1, then=OSError(errno.EIO, "I/O error"))
    with pytest.raises(OSError) as raised:
        _windows_stage_lock(monkeypatch, tmp_path, fake)
    assert raised.value.errno == errno.EIO and len(fake.calls) == 2


def test_complete_means_every_segment_from_the_values_analysis(lib: Lib, make_mik_store, write_verdicts) -> None:
    """if a duplicate whose series mixes the value's analysis with an older one counts as complete then broken."""
    lib.track("a", "one/x.mp3", title="Tamborel", artist="Quilla Ventrosk", duration_ms=200_000)
    lib.track("b", "two/x.mp3", title="Tamborel", artist="Quilla Ventrosk", duration_ms=200_300)
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    staged = backfill.stage(lib.library, live=True, share_across_duplicates=True, ingest_root=lib.ingest, now=NOW)
    batch = Path(staged["batch_dir"])
    lib.energy("b", 4)
    lib.series("b")  # same stamp as the value
    lib.series("b", modified_at="2025-06-01T00:00:00+00:00", start_index=len(SEGMENTS))  # plus older segments
    store = make_mik_store([{"path": str(batch / "x.mp3"), "energy": 6.0, "segments": SEGMENTS}])
    backfill.collect(lib.library, backfill.CollectRequest(batch_dir=batch, store=store, live=True, share_across_duplicates=True))
    assert dict(_energy_rows(lib))[_sid("b")] == "6"


def test_complete_means_value_and_series_from_one_analysis(lib: Lib, make_mik_store, write_verdicts) -> None:
    """if a duplicate whose MIK value and series come from two analyses counts as complete and is never refilled then broken."""
    lib.track("a", "one/x.mp3", title="Tamborel", artist="Quilla Ventrosk", duration_ms=200_000)
    lib.track("b", "two/x.mp3", title="Tamborel", artist="Quilla Ventrosk", duration_ms=200_300)
    write_verdicts({field: "passed" for field in loader.GATED_FIELDS})
    staged = backfill.stage(lib.library, live=True, share_across_duplicates=True, ingest_root=lib.ingest, now=NOW)
    batch = Path(staged["batch_dir"])
    lib.energy("b", 4)
    lib.series("b", modified_at="2025-06-01T00:00:00+00:00")  # a series left over from an older analysis
    store = make_mik_store([{"path": str(batch / "x.mp3"), "energy": 6.0, "segments": SEGMENTS}])
    backfill.collect(lib.library, backfill.CollectRequest(batch_dir=batch, store=store, live=True, share_across_duplicates=True))
    assert dict(_energy_rows(lib))[_sid("b")] == "6"


_APPEND_WORKER = """
import sys
from pathlib import Path
from apps.mik.backfill import _record_collected
folder, writer, start = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
while not start.exists():
    pass
for n in range(200):
    _record_collected(folder, {f"/music/{writer}/{n:04d}-" + "x" * 120 + ".mp3"})
"""


def test_overlapping_collects_keep_each_others_closed_sources(tmp_path: Path) -> None:
    """if collect processes settling one batch at once lose, truncate or interleave each other's closed sources then broken."""
    start = tmp_path / "go"
    writers = [
        subprocess.Popen([sys.executable, "-c", _APPEND_WORKER, str(tmp_path), name, str(start)], cwd=Path(__file__).parents[2])
        for name in ("w1", "w2", "w3", "w4")
    ]
    start.touch()  # release every writer at once
    assert [writer.wait(timeout=120) for writer in writers] == [0, 0, 0, 0]
    expected = {f"/music/{name}/{n:04d}-" + "x" * 120 + ".mp3" for name in ("w1", "w2", "w3", "w4") for n in range(200)}
    assert backfill._collected_lines(tmp_path) == expected  # 800 lines, none lost and none spliced
