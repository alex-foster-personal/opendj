"""Smoke tests for ``python -m apps.shared.state.cli init`` + ``stats``."""
from __future__ import annotations

from pathlib import Path

import pytest

from apps.shared.state import cli as state_cli
from apps.shared.state import schema as state_schema

pytestmark = pytest.mark.requirement("INFRA-01")


def test_init_creates_state_db(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    target = tmp_path / "s.db"
    rc = state_cli.main(["--db", str(target), "init"])
    assert rc == 0
    assert target.exists()
    out = capsys.readouterr().out
    assert f"schema version: {state_schema.SCHEMA_VERSION}" in out
    assert "tracks: 0" in out


def test_init_is_idempotent(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    target = tmp_path / "s.db"
    assert state_cli.main(["--db", str(target), "init"]) == 0
    capsys.readouterr()
    assert state_cli.main(["--db", str(target), "init"]) == 0
    out = capsys.readouterr().out
    assert "opened" in out
    assert f"schema version: {state_schema.SCHEMA_VERSION}" in out


def test_stats_on_empty_db(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    target = tmp_path / "s.db"
    state_cli.main(["--db", str(target), "init"])
    capsys.readouterr()
    rc = state_cli.main(["--db", str(target), "stats"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "tracks: 0" in out
    assert "[events] 0 row" in out


def test_stats_missing_db_exits_one(tmp_path: Path) -> None:
    target = tmp_path / "nope.db"
    rc = state_cli.main(["--db", str(target), "stats"])
    assert rc == 1
