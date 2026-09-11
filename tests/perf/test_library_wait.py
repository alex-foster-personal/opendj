"""PERFBATCH-01 wait-time reporter: six KPIs, honest present denominators."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from scripts.perf import library_wait as mod

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "perf" / "library-wait"
CHECKED = "2026-09-11T07:06:49+00:00"


def _availability(
    present: tuple[str, ...] = ("present-a", "present-b"),
    absent: tuple[str, ...] = ("absent-c",),
    awaiting: tuple[str, ...] = (),
) -> mod.Availability:
    return mod.Availability(
        present=len(present),
        awaiting_volume=len(awaiting),
        absent=len(absent),
        streaming=0,
        present_ids=frozenset(present),
        checked_at=CHECKED,
    )


def _write_json(path: Path, payload: dict, mtime: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    if mtime is not None:
        os.utime(path, (mtime, mtime))




def _write_index(path: Path, *, last_pending: int, docs_indexed: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE lyrics_index_meta ("
            "id INTEGER PRIMARY KEY, docs_indexed INTEGER NOT NULL,"
            "last_pending INTEGER NOT NULL, created_ms INTEGER,"
            "last_committed_ms INTEGER, committed_batches INTEGER)"
        )
        conn.execute(
            "INSERT INTO lyrics_index_meta("
            "id, docs_indexed, last_pending, created_ms, last_committed_ms,"
            " committed_batches) VALUES (1, ?, ?, 1750000000000, 1750000004000, 1)",
            (docs_indexed, last_pending),
        )
        conn.commit()
    finally:
        conn.close()


def test_snapshot_names_the_six_kpis(tmp_path: Path) -> None:
    snap = mod.capture_snapshot(tmp_path, capture_id="c1", machine="test", now=CHECKED)
    assert [figure.kpi for figure in snap.figures] == list(mod.KPI_NAMES)
    assert {figure.capture_id for figure in snap.figures} == {"c1"}
    assert snap.capture_id == "c1"


def test_numeric_value_requires_present_denominator() -> None:
    figure = mod.Figure(
        kpi="stems_per_track_wall_s",
        unit="s",
        value=9.87,
        note="farm window n=92",
        measured=True,
        capture_id="c1",
    )
    with pytest.raises(mod.InstrumentError, match="denominator=present"):
        mod.validate_figure(figure)


def test_quoting_all_library_rows_as_remaining_fails() -> None:
    present = frozenset({"present-a", "present-b"})
    have = frozenset({"present-a"})
    remaining = mod.remaining_present_without(present, have)
    assert remaining == frozenset({"present-b"})
    dishonest = present | frozenset({"absent-c"})
    dishonest_remaining = dishonest - have
    with pytest.raises(mod.InstrumentError, match="non-present"):
        mod.check_remaining_is_present_only(dishonest_remaining, present)


def test_numeric_note_that_quotes_tracks_row_count_fails() -> None:
    figure = mod.Figure(
        kpi="stems_library_eta_s",
        unit="s",
        value=10.0,
        note="denominator=present 2; denominator=tracks 8355",
        measured=True,
        capture_id="c1",
    )
    with pytest.raises(mod.InstrumentError, match="tracks"):
        mod.validate_figure(figure)


def test_absent_state_db_withholds_library_scale_not_zero(tmp_path: Path) -> None:
    snap = mod.capture_snapshot(tmp_path, capture_id="c1", machine="test", now=CHECKED)
    eta = snap.by_name("stems_library_eta_s")
    lyrics_eta = snap.by_name("lyrics_library_eta_s")
    assert eta.value is None and eta.measured is False and eta.status == "withheld"
    assert lyrics_eta.value is None and lyrics_eta.measured is False
    assert eta.value != 0
    assert "denominator=present" in eta.note




def test_remaining_without_bundle_uses_only_present_ids(tmp_path: Path) -> None:
    stems = tmp_path / "state" / "stems"
    (stems / "present-a").mkdir(parents=True)
    (stems / "present-a" / "manifest.json").write_text("{}")
    (stems / "absent-c").mkdir(parents=True)
    (stems / "absent-c" / "manifest.json").write_text("{}")
    avail = _availability()
    remaining = mod.stems_remaining_present(tmp_path, avail)
    assert remaining == frozenset({"present-b"})
    assert "absent-c" not in remaining


def test_injected_delay_moves_wall_and_throughput(tmp_path: Path) -> None:
    def window(root: Path, last_gap_s: float) -> mod.WaitSnapshot:
        cache = root / "state" / "lyrics-cache"
        _write_json(cache / "a.json", {"schema": 1}, 1000.0)
        _write_json(cache / "b.json", {"schema": 1}, 1010.0)
        _write_json(cache / "c.json", {"schema": 1}, 1020.0 + last_gap_s)
        return mod.capture_snapshot(root, capture_id="d", machine="test", now=CHECKED)

    baseline = window(tmp_path / "base", 0.0)
    delayed = window(tmp_path / "slow", 30.0)
    base_wall = baseline.by_name("lyrics_per_track_wall_s")
    slow_wall = delayed.by_name("lyrics_per_track_wall_s")
    base_rate = baseline.by_name("lyrics_queue_throughput_per_h")
    slow_rate = delayed.by_name("lyrics_queue_throughput_per_h")
    assert base_wall.measured and slow_wall.measured
    assert base_wall.value is not None and slow_wall.value is not None
    assert slow_wall.value > base_wall.value
    assert slow_rate.value is not None and base_rate.value is not None
    assert slow_rate.value < base_rate.value


def test_lyrics_eta_names_index_pending_when_drain_is_in_front(tmp_path: Path) -> None:
    cache = tmp_path / "state" / "lyrics-cache"
    _write_json(cache / "present-a.json", {"schema": 1}, 1000.0)
    _write_json(cache / "present-b.json", {"schema": 1}, 1010.0)
    _write_index(tmp_path / "state" / "lyrics-index.db", last_pending=3)
    avail = _availability()
    snap = mod.capture_snapshot(
        tmp_path,
        capture_id="ly",
        machine="test",
        now=CHECKED,
        availability=avail,
    )
    eta = snap.by_name("lyrics_library_eta_s")
    assert eta.measured is True
    assert "index" in eta.note
    assert "last_pending=3" in eta.note
    assert "denominator=present 2" in eta.note


def test_append_refuses_numeric_without_present_denominator(tmp_path: Path) -> None:
    ledger = tmp_path / "kpi-ledger.json"
    ledger.write_text(json.dumps({"schema_version": 1, "entries": []}))
    figure = mod.Figure(
        kpi="stems_per_track_wall_s",
        unit="s",
        value=1.0,
        note="no denom",
        measured=True,
        capture_id="c1",
    )
    with pytest.raises(mod.InstrumentError, match="denominator=present"):
        mod.append_ledger_rows(ledger, [figure], date="2026-09-11", machine="test")


def test_cli_json_exits_zero_when_values_are_withheld(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = mod.main(["--data-dir", str(tmp_path), "--json", "--capture-id", "cli"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["capture_id"] == "cli"
    names = [row["kpi"] for row in payload["kpis"]]
    assert names == list(mod.KPI_NAMES)
    assert all(row["capture_id"] == "cli" for row in payload["kpis"])


def test_require_measured_exits_nonzero_on_empty_window(tmp_path: Path) -> None:
    rc = mod.main(
        ["--data-dir", str(tmp_path), "--json", "--require-measured", "--capture-id", "x"]
    )
    assert rc == 1


def test_fixture_dir_declares_present_vs_absent() -> None:
    doc = json.loads((FIXTURE / "availability.json").read_text())
    assert doc["present"] == ["present-a", "present-b"]
    assert doc["absent"] == ["absent-c"]
    assert (FIXTURE / "lyrics-cache" / "present-a.json").is_file()
