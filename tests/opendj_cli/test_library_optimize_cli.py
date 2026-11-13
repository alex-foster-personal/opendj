"""``opendj library optimize`` is the CLI twin of the optimize route (LIBM-126, issue #12332, agent parity).

[if] opendj library optimize runs without --yes, or its --json answer is not parseable [then] fail, [else stop].
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED
from apps.opendj_cli.__main__ import main
from apps.shared.state import db as state_db
from apps.shared.state import db_optimize


pytestmark = pytest.mark.requirement("LIBM-14744")


@pytest.fixture
def library_dir(tmp_path: Path) -> Path:
    """A temp data dir with an initialized state.db (fixture only, never the real library)."""
    data_dir = tmp_path / "data"
    state_db.open_rw(db_optimize.library_db_path(data_dir)).close()
    return data_dir


def test_without_yes_exits_1_and_runs_nothing(
    library_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] opendj library optimize lacks --yes [then] exit 1 confirm_required and the core is never called, [else stop]."""
    calls: list[Path] = []
    monkeypatch.setattr(db_optimize, "optimize_library_db", calls.append)
    assert main(["--json", "library", "optimize", "--data-dir", str(library_dir)]) == EXIT_FAILED
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "confirm_required"
    assert calls == []


def test_json_output_reports_completion(library_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] the CLI optimizes with --json [then] exit 0 and a JSON summary naming the optimized db, [else stop]."""
    assert main(["--json", "library", "optimize", "--data-dir", str(library_dir), "--yes"]) == EXIT_CONFIRMED
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "completed"
    assert summary["db_path"] == str(db_optimize.library_db_path(library_dir))
    assert summary["bytes_after"] > 0


def test_missing_library_db_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """[if] the CLI targets a data dir with no state.db [then] exit 1 library_db_missing, [else stop]."""
    assert main(["--json", "library", "optimize", "--data-dir", str(tmp_path / "none"), "--yes"]) == EXIT_FAILED
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "library_db_missing"
