"""Tests for ``open-dj-tool conformance`` (Phase 15 CLI finisher)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from apps.open_dj.cli import main


class _FakeCompleted:
    def __init__(self, rc: int = 0) -> None:
        self.returncode = rc


@pytest.mark.requirement("OPEN-01")
class TestConformance:

    def test_runs_all_fixtures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []

        def _fake_run(cmd, check=False):
            calls.append(list(cmd))
            return _FakeCompleted(0)

        monkeypatch.setattr(subprocess, "run", _fake_run)
        rc = main(["conformance"])
        assert rc == 0
        assert calls, "pytest should have been invoked"
        cmd = calls[0]
        assert cmd[:3] == [sys.executable, "-m", "pytest"]
        assert "-m" in cmd and "conformance" in cmd
        # No -k filter when no fixture dir is given.
        assert "-k" not in cmd

    def test_runs_single_fixture(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls: list[list[str]] = []

        def _fake_run(cmd, check=False):
            calls.append(list(cmd))
            return _FakeCompleted(0)

        monkeypatch.setattr(subprocess, "run", _fake_run)

        fixture = tmp_path / "01-my-fixture"
        fixture.mkdir()

        rc = main(["conformance", str(fixture)])
        assert rc == 0
        cmd = calls[0]
        assert "-k" in cmd
        assert cmd[cmd.index("-k") + 1] == "01-my-fixture"

    def test_missing_fixture_exits_1(self, tmp_path: Path) -> None:
        rc = main(["conformance", str(tmp_path / "does-not-exist")])
        assert rc == 1

    def test_forwards_pytest_args(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[list[str]] = []
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, check=False: (calls.append(list(cmd)) or _FakeCompleted(0)),
        )
        # Use the ``=`` form: with plain ``--pytest-arg -vv`` argparse treats
        # ``-vv`` as an option of the conformance subparser and errors out.
        rc = main(["conformance", "--pytest-arg=-vv", "--pytest-arg=--tb=short"])
        assert rc == 0
        cmd = calls[0]
        assert "-vv" in cmd
        assert "--tb=short" in cmd

    def test_pytest_failure_propagates_return_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            subprocess, "run", lambda cmd, check=False: _FakeCompleted(1)
        )
        rc = main(["conformance"])
        assert rc == 1

    def test_pytest_launch_failure_returns_1(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail(cmd, check=False):
            raise FileNotFoundError("pytest not on PATH")

        monkeypatch.setattr(subprocess, "run", _fail)
        rc = main(["conformance"])
        assert rc == 1


@pytest.mark.requirement("OPEN-01")
def test_end_to_end_single_fixture(tmp_path: Path, monkeypatch) -> None:
    """Smoke: invoking the conformance subcommand against the real corpus
    fixture produces a zero-or-pytest-return exit and fires pytest."""
    calls: list[list[str]] = []

    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, check=False: (calls.append(list(cmd)) or _FakeCompleted(0)),
    )
    rc = main([
        "conformance",
        "tests/fixtures/conformance/01-ascii-baseline",
    ])
    assert rc == 0
    assert calls
    assert "01-ascii-baseline" in calls[0]
