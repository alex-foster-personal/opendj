"""Acceptance tests for the v10 state.db migration runbook (issue #2080).

[if] the v10 lyrics migration runs [then] dry-run writes nothing, live runs once, [else stop].
"""
from __future__ import annotations

import hashlib
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apps.lyrics import store
from apps.shared.state import db as state_db
from scripts.lyrics_migrate_state_v10 import (
    KARAOKE_TARGET_VERSION,
    apply_ladder_to_v10,
    assert_stale_index_gone,
    drop_stale_indexes,
    run,
)
from tests.lyrics.test_legacy_words import (
    _LEGACY_WORDS,
    _apply_ladder_to_v8,
    _seed_legacy_db,
)

from .conftest import use_local_mode

pytestmark = pytest.mark.requirement("LYR-04")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _utc_backup_name() -> str:
    date = datetime.now(UTC).strftime("%Y-%m-%d")
    return f"state.db.bak-{date}"


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


#-----------------------------------------------------------------------------
# AC 1: dry-run changes no byte
#-----------------------------------------------------------------------------
def test_dry_run_prints_plan_and_counts_without_writing(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_local_mode(monkeypatch)
    db_path = data_dir / "state" / "state.db"
    _seed_legacy_db(db_path, words=_LEGACY_WORDS)
    before = _sha256(db_path)
    code = run(db_path, live=False)
    captured = capsys.readouterr().out
    assert code == 0
    assert _sha256(db_path) == before
    assert not list(db_path.parent.glob("state.db.bak-*"))
    assert "[DRY-RUN]" in captured
    assert "would backup" in captured
    assert "would apply ladder" in captured
    assert "3/3 legacy verdicts" in captured or "3 legacy verdicts" in captured


#-----------------------------------------------------------------------------
# AC 2: live on fixture
#-----------------------------------------------------------------------------
def test_live_migrates_fixture_and_second_run_is_no_op(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    use_local_mode(monkeypatch)
    db_path = data_dir / "state" / "state.db"
    _seed_legacy_db(db_path, words=_LEGACY_WORDS)
    first = run(db_path, live=True)
    assert first == 0
    backup = db_path.parent / _utc_backup_name()
    assert backup.is_file()
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0] == (
            KARAOKE_TARGET_VERSION
        )
        assert conn.execute("SELECT COUNT(*) FROM lyric_verdict").fetchone()[0] == 3
        tables = _tables(conn)
        assert "lyric_verdict_legacy" in tables
        assert "lyric_word_legacy" in tables
    finally:
        conn.close()
    conn = state_db.open_rw(db_path, apply_schema=False)
    try:
        assert store.get_verdict(conn, "sid-a") is not None
    finally:
        conn.close()
    after_first = _sha256(db_path)
    capsys.readouterr()
    second = run(db_path, live=True)
    captured = capsys.readouterr().out
    assert second == 0
    assert "already at schema v10" in captured
    assert _sha256(db_path) == after_first


#-----------------------------------------------------------------------------
# AC 3: stale index aborts before ladder
#-----------------------------------------------------------------------------
def test_stale_index_aborts_before_ladder(tmp_path: Path) -> None:
    db_path = tmp_path / "stale" / "state" / "state.db"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        _apply_ladder_to_v8(conn)
        conn.execute(
            "CREATE TABLE lyric_verdict (stable_id TEXT PRIMARY KEY, "
            "pct_witness_red REAL)"
        )
        conn.execute(
            "CREATE INDEX idx_lyric_verdict_red ON lyric_verdict(pct_witness_red DESC)"
        )
        conn.execute("ALTER TABLE lyric_verdict RENAME TO lyric_verdict_legacy")
        conn.commit()
        version_before = conn.execute(
            "SELECT MAX(version) FROM schema_meta"
        ).fetchone()[0]
        with pytest.raises(RuntimeError, match="idx_lyric_verdict_red"):
            assert_stale_index_gone(conn)
        assert conn.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0] == (
            version_before
        )
    finally:
        conn.close()


def test_dropping_stale_index_lets_ladder_succeed(tmp_path: Path) -> None:
    db_path = tmp_path / "index" / "state" / "state.db"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        _apply_ladder_to_v8(conn)
        conn.execute(
            "CREATE TABLE lyric_verdict (stable_id TEXT PRIMARY KEY, "
            "pct_witness_red REAL)"
        )
        conn.execute(
            "CREATE INDEX idx_lyric_verdict_red ON lyric_verdict(pct_witness_red DESC)"
        )
        conn.execute("ALTER TABLE lyric_verdict RENAME TO lyric_verdict_legacy")
        drop_stale_indexes(conn)
        assert_stale_index_gone(conn)
        apply_ladder_to_v10(conn)
        rows = conn.execute(
            "SELECT name, tbl_name FROM sqlite_master WHERE type='index' AND "
            "name = 'idx_lyric_verdict_red'"
        ).fetchall()
        assert [(str(name), str(table)) for name, table in rows] == [
            ("idx_lyric_verdict_red", "lyric_verdict")
        ]
    finally:
        conn.close()


#-----------------------------------------------------------------------------
# AC 4: CLI --help and doc commands
#-----------------------------------------------------------------------------
def test_runbook_help_exits_zero() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.lyrics_migrate_state_v10", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0


def test_live_and_dry_run_together_exit_non_zero() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.lyrics_migrate_state_v10",
            "--db-path",
            "/tmp/none.db",
            "--live",
            "--dry-run",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0


def test_missing_db_path_exits_non_zero() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.lyrics_migrate_state_v10", "--help"],
        check=False,
    )
    assert proc.returncode == 0
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.lyrics_migrate_state_v10"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0


@pytest.mark.parametrize(
    "command",
    [
        [sys.executable, "-m", "apps.lyrics", "ingest-state", "--help"],
        [sys.executable, "-m", "apps.lyrics", "migrate-legacy-words", "--help"],
        [sys.executable, "-m", "apps.lyrics", "purge", "--help"],
        [sys.executable, "-m", "apps.sync_hub", "policy", "set", "--help"],
        [sys.executable, "scripts/bench/lyrics_kpi_append.py", "--help"],
    ],
)
def test_documented_commands_help_exits_zero(command: list[str]) -> None:
    proc = subprocess.run(command, check=False, capture_output=True, text=True)
    assert proc.returncode == 0
