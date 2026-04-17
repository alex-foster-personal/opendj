"""SMART-02 -- ``python -m apps.smartlists.refresh`` CLI tests."""
from __future__ import annotations

import io
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from apps.shared.pairings import ensure_phase08_tables
from apps.smartlists import refresh as cli_refresh
from apps.smartlists.cli import create as cli_create


pytestmark = pytest.mark.requirement("SMART-02")


FIXTURE_DIR = (
    Path(__file__).resolve().parents[1] / "fixtures" / "smartlists"
)


_PHASE5_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS tracks (
        stable_id       TEXT PRIMARY KEY,
        stable_id_tier  TEXT NOT NULL,
        title           TEXT,
        artists_json    TEXT,
        album           TEXT,
        isrc            TEXT,
        duration_ms     INTEGER,
        file_path       TEXT,
        content_hash    TEXT,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS track_fields (
        stable_id    TEXT NOT NULL,
        field_name   TEXT NOT NULL,
        value_json   TEXT NOT NULL,
        source       TEXT NOT NULL,
        confidence   REAL,
        modified_at  TEXT NOT NULL,
        PRIMARY KEY (stable_id, field_name)
    )
    """,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "state.db"
    # Pre-populate so the CLI can evaluate rules against real tables.
    conn = sqlite3.connect(str(p), isolation_level=None)
    try:
        for stmt in _PHASE5_DDL:
            conn.execute(stmt)
        ensure_phase08_tables(conn)
    finally:
        conn.close()
    return p


def _seed_smartlist(db_path: Path, name: str, rule_file: str) -> None:
    cli_create.main([
        "--db", str(db_path),
        "--name", name,
        "--rule", str(FIXTURE_DIR / rule_file),
    ])


def test_refresh_default_is_dry_run(db_path: Path) -> None:
    _seed_smartlist(db_path, "Energetic", "high_energy.json")
    buf = io.StringIO()
    rc = cli_refresh.main(["--db", str(db_path)], out=buf)
    assert rc == 0
    assert "Energetic" in buf.getvalue()


def test_refresh_live_requires_risk_flag(
    db_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    _seed_smartlist(db_path, "x", "high_energy.json")
    rc = cli_refresh.main(["--db", str(db_path), "--live"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "i-understand-the-risks" in err


def test_refresh_live_with_flag_runs(
    db_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    _seed_smartlist(db_path, "x", "high_energy.json")
    rc = cli_refresh.main([
        "--db", str(db_path), "--live", "--i-understand-the-risks",
    ])
    assert rc == 0


def test_refresh_name_not_found_returns_1(
    db_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    _seed_smartlist(db_path, "a", "high_energy.json")
    rc = cli_refresh.main(["--db", str(db_path), "--name", "nope"])
    assert rc == 1
