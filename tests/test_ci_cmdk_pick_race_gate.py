"""The Cmd K pick-race CI gate helper resolves its programs by path and says what is missing."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

from scripts import ci_cmdk_pick_race_gate as gate

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_resolve_names_the_missing_program(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # An empty PATH: the lookup is the real one, and nothing on it is called pnpm.
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(SystemExit, match="pnpm is not on PATH"):
        gate.resolve("pnpm")


@pytest.mark.skipif(os.name == "nt", reason="the stand-in bash is a POSIX shell script; Windows has no bash to stand in for")
def test_the_run_is_launched_through_a_resolved_bash_with_its_ports_and_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A real executable named bash on a PATH that holds only its directory: the lookup is the real one.
    bash = tmp_path / "bash"
    bash.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    bash.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    pnpm = "/resolved/pnpm"
    argv = gate.pick_race_argv(pnpm)
    assert os.path.samefile(argv[0], bash)
    # The lock wrapper, the reaper, both ports, and the pnpm path verbatim after the separator.
    assert argv[1] == str(REPOSITORY_ROOT / "scripts" / "ci_host_lock.sh")
    assert argv[2:4] == ["cmdk-pick-race", str(REPOSITORY_ROOT / "scripts" / "ci_reap_port_holders.sh")]
    assert argv[4:8] == ["5336", "8716", "--", pnpm]
    assert argv[8:] == [
        "exec",
        "playwright",
        "test",
        "--config",
        "tests/e2e/playwright.cmdk-pick-race.config.ts",
        "--output",
        "test-results/cmdk-pick-race",
    ]
    assert (REPOSITORY_ROOT / "scripts" / "ci_host_lock.sh").is_file()
    assert (REPOSITORY_ROOT / "scripts" / "ci_reap_port_holders.sh").is_file()


def test_the_launcher_locks_and_reaps_the_ports_its_vite_config_pins() -> None:
    """The step no longer names its config in YAML, so the lock test cannot find it: pin the ports here instead."""
    # One literal string, not path segments: the CI planner reads a joined "tests" segment as a
    # computed test-helper load, which marks every global helper unplaceable (test_ci_plan_helpers).
    vite = (REPOSITORY_ROOT / "apps/webui/frontend/tests/e2e/vite.cmdk-pick-race.config.ts").read_text(encoding="utf-8")
    pinned = tuple(re.findall(r"CMDK_PICK_RACE_\w*PORT = (\d{4,5});", vite))
    assert pinned == gate.PICK_RACE_PORTS, f"the vite config pins {pinned}, the gate reaps {gate.PICK_RACE_PORTS}"


def test_a_missing_bash_names_itself(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # An empty PATH: the lookup is the real one, and no bash is on it.
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(SystemExit, match="bash is not on PATH"):
        gate.pick_race_argv("/resolved/pnpm")


def test_a_failing_step_stops_the_gate_with_its_exit_code() -> None:
    # A real child process that exits 3, not a stand-in for subprocess.run.
    with pytest.raises(SystemExit, match="exited 3"):
        gate.run([sys.executable, "-c", "raise SystemExit(3)"], REPOSITORY_ROOT, dict(os.environ))
