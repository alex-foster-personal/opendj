"""Traktor history NMLs into Open DJ's play log (DJIMPORT-10069).

[if] History/ holds a session NML with PLAYEDPUBLIC entries [then] matched tracks become track_played events idempotently, [else stop].

Regression one-liners:
  - if STARTDATE / STARTTIME decode to the wrong UTC instant then play times are wrong
  - if an entry with PLAYEDPUBLIC 0 is imported then cued-but-unplayed tracks count as plays
  - if unmatched entries are not counted then a failed match is silent
  - if a re-run appends rows then the play count inflates on every import
  - if one non-NML file stops the import then a stray file blocks every session
"""
from __future__ import annotations

import dataclasses
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from apps.adapters import library_import
from apps.adapters.traktor.history import decode_start
from apps.shared.state import db as state_db
from apps.shared.state import play_log

pytestmark = [pytest.mark.requirement("DJIMPORT-10069")]

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "traktor_nml" / "history_minimal.nml"
HISTORY_NAME = "history_2019y12m11d_17h00m00s.nml"
NY = ZoneInfo("America/New_York")
SIDS = {"a.mp3": "a" * 40, "b.mp3": "b" * 40, "c.mp3": "c" * 40}


def _env(tmp_path: Path, seeded: tuple[str, ...] = ("a.mp3", "b.mp3", "c.mp3")) -> SimpleNamespace:
    traktor = tmp_path / "Traktor 3.11"
    (traktor / "History").mkdir(parents=True)
    shutil.copy(FIXTURE, traktor / "History" / HISTORY_NAME)
    state = tmp_path / "state.db"
    conn = state_db.open_rw(state)
    try:
        for name in seeded:
            conn.execute(
                "INSERT INTO tracks (stable_id, stable_id_tier, title, file_path, created_at, updated_at) "
                "VALUES (?, 'inferred', ?, ?, '2026-01-01', '2026-01-01')",
                (SIDS[name], name, f"/Music/Set/{name}"),
            )
        conn.commit()
    finally:
        conn.close()
    return SimpleNamespace(traktor=traktor, state=state, volumes=tmp_path / "Volumes")


def _import(env: SimpleNamespace, *, dry_run: bool = False) -> library_import.TraktorHistoryImportReport:
    conn = state_db.open_rw(env.state)
    try:
        return library_import.import_traktor_history(
            conn,
            env.traktor,
            zone=NY,
            dry_run=dry_run,
            actor="test",
            resolve_path=library_import.traktor_path_resolver(volumes_root=env.volumes),
        )
    finally:
        conn.close()


def _rows(state: Path) -> list[tuple[str, str, dict]]:
    conn = state_db.open_ro(state)
    try:
        rows = conn.execute(
            "SELECT ts, stable_id, payload_json FROM events WHERE kind = ? ORDER BY ts", (play_log.PLAY_KIND,)
        ).fetchall()
    finally:
        conn.close()
    return [(ts, sid, json.loads(payload)) for ts, sid, payload in rows]


def test_decode_start_pins_known_value() -> None:
    winter = decode_start(132320267, 61204, NY)
    assert winter == datetime(2019, 12, 11, 22, 0, 4, tzinfo=UTC)
    summer = decode_start(2019 * 65536 + 7 * 256 + 1, 61204, NY)
    assert summer == datetime(2019, 7, 1, 21, 0, 4, tzinfo=UTC), "DST offset not applied"


def test_decode_start_rejects_impossible_values() -> None:
    with pytest.raises(ValueError):
        decode_start(2019 * 65536 + 13 * 256 + 1, 0, NY)
    with pytest.raises(ValueError):
        decode_start(132320267, 86_400, NY)


def test_three_entries_two_public_yield_two_plays(tmp_path: Path) -> None:
    env = _env(tmp_path)
    report = _import(env)
    assert (report.entries_seen, report.entries_not_public, report.plays_added) == (3, 1, 2)
    rows = _rows(env.state)
    assert [(ts, sid) for ts, sid, _ in rows] == [
        ("2019-12-11T22:00:04.000000+00:00", SIDS["a.mp3"]),
        ("2019-12-11T22:06:00.000000+00:00", SIDS["c.mp3"]),
    ]
    assert [p["play_id"] for _, _, p in rows] == [f"traktor-history:{HISTORY_NAME}:0", f"traktor-history:{HISTORY_NAME}:2"]
    assert [p["deck"] for _, _, p in rows] == [1, 2]
    assert {p["source"] for _, _, p in rows} == {"traktor-history"}


def test_unmatched_entries_are_counted(tmp_path: Path) -> None:
    env = _env(tmp_path, seeded=("a.mp3",))
    report = _import(env)
    assert (report.plays_matched, report.plays_unmatched, report.plays_added) == (1, 1, 1)
    assert len(_rows(env.state)) == 1


def test_rerun_adds_nothing(tmp_path: Path) -> None:
    env = _env(tmp_path)
    _import(env)
    again = _import(env)
    assert (again.plays_added, again.plays_already_logged) == (0, 2)
    assert len(_rows(env.state)) == 2


def test_same_play_id_under_another_track_is_not_readded(tmp_path: Path) -> None:
    env = _env(tmp_path)
    play = play_log.ImportedPlay(
        "a" * 40,
        "traktor-history:x.nml:0",
        "2019-12-11T22:00:04.000000+00:00",
        deck=1,
        source="traktor-history",
        origin="x.nml",
        played_s=1.0,
    )
    conn = state_db.open_rw(env.state)
    try:
        assert play_log.import_plays(conn, [play], actor="t") == 1
        moved = dataclasses.replace(play, stable_id="b" * 40)
        assert play_log.import_plays(conn, [moved], actor="t") == 0
        conn.commit()
    finally:
        conn.close()


def test_non_nml_file_is_named_and_rest_import(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env = _env(tmp_path)
    (env.traktor / "History" / "notes.txt").write_text("hello")
    (env.traktor / "History" / "history_broken.nml").write_text("not xml <")
    report = _import(env)
    assert [name for name, _ in report.files_rejected] == ["history_broken.nml", "notes.txt"]
    assert report.files_read == [HISTORY_NAME]
    assert report.plays_added == 2
    code = library_import.main(["traktor-history", "--source", str(env.traktor), "--zone", "America/New_York", "--db", str(env.state)])
    out = capsys.readouterr()
    assert code == 0
    assert "notes.txt" in out.out and "history_broken.nml" in out.out
    assert "warning: skipped notes.txt" in out.err


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    env = _env(tmp_path)
    assert _import(env, dry_run=True).plays_added == 2
    assert _rows(env.state) == []


def test_cli_traktor_history_writes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env = _env(tmp_path)
    argv = ["traktor-history", "--source", str(env.traktor), "--zone", "America/New_York", "--write", "--db", str(env.state)]
    assert library_import.main(argv) == 0
    assert "plays added:          2" in capsys.readouterr().out
    assert library_import.main(argv) == 0
    assert "plays added:          0" in capsys.readouterr().out
    assert len(_rows(env.state)) == 2


def test_missing_history_dir_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = library_import.main(["traktor-history", "--source", str(tmp_path), "--db", str(tmp_path / "s.db")])
    assert code == 1
    assert "no Traktor history found" in capsys.readouterr().err
