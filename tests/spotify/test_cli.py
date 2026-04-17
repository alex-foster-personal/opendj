"""CLI surface tests for apps.spotify.__main__."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.shared.state import schema as state_schema
from apps.spotify.__main__ import main as cli_main


@pytest.fixture
def state_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state.db"
    conn = sqlite3.connect(path, isolation_level=None)
    state_schema.apply_migrations(conn)
    conn.close()
    monkeypatch.setattr("apps.shared.paths.STATE_DB", path)
    monkeypatch.setattr("apps.shared.state.paths.STATE_DB", path)
    return path


@pytest.mark.requirement("CAT-01")
def test_cli_help_exits_zero(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli_main(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "import" in out and "rematch" in out


@pytest.mark.requirement("CAT-01")
def test_cli_import_rejects_invalid_url(capsys: pytest.CaptureFixture) -> None:
    rc = cli_main(["import", "not-a-playlist"])
    assert rc == 3
    assert "ERROR" in capsys.readouterr().err


@pytest.mark.requirement("CAT-01")
def test_cli_import_live_without_flag_refuses(capsys: pytest.CaptureFixture) -> None:
    rc = cli_main(["import", "37i9dQZF1DXcBWIGoYBM5M", "--live"])
    assert rc == 3
    assert "i-understand-the-risks" in capsys.readouterr().err


@pytest.mark.requirement("CAT-01")
def test_cli_rematch_with_no_pending(
    capsys: pytest.CaptureFixture, state_db_path: Path,
) -> None:
    rc = cli_main(["rematch", "--playlist-id", "37i9dQZF1DXcBWIGoYBM5M"])
    assert rc == 0
    assert "rematch" in capsys.readouterr().out
