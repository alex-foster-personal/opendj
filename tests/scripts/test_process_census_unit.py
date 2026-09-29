"""UNIT tests of the census/reaper functional core, on synthetic process tables.

These pin classification and reap-outcome logic cheaply and deterministically
(functional core, imperative shell). They are NOT DEVOPS-17 release evidence
and carry no requirement marker on purpose: the evidence is the real-process
suite in ``test_orphan_reaper.py`` (real orphans, real subreaper adoption,
real reaps).

Regression lines:
  - if a service id that merely mentions codex/claude/cursor/grok is reapable then broken
  - if an orphan whose own environment cannot be read is reapable then broken
  - if a `systemd --user` adoptee is not orphaned, or a tmux child is then broken
  - if reap reports a target it never signalled as killed then broken
"""

from __future__ import annotations

import subprocess
import sys

from scripts.orphan_reaper import reap
from scripts.process_census import Proc, Row, classify


def _proc(pid: int, ppid: int, command: str, **extra: object) -> Proc:
    """A synthetic process whose environment WAS read, unless the test says otherwise."""
    extra.setdefault("env_readable", True)
    return Proc(pid, ppid, pid, 3600, "S", "dev", command, start=f"t{pid}", **extra)  # type: ignore[arg-type]


def _orphan_verdict(own_env: dict[str, str], *, env_readable: bool = True) -> str:
    """Verdict for an orphaned, test-shaped server run from a checkout inside an agent session."""
    procs = {
        1: _proc(1, 0, "/sbin/init"),
        50: _proc(
            50,
            1,
            "uvicorn apps.engine_core",
            env={"CLAUDECODE": "1", **own_env},
            cwd="/home/dev/music-dj-tools",
            env_readable=env_readable,
        ),
    }
    return {r.pid: r for r in classify(procs)}[50].verdict


def test_a_service_id_is_protected_whatever_names_it_contains() -> None:
    """Only an agent SESSION's own id namespace is provenance; a service whose
    name merely mentions an agent (codex, claude...) is still a service."""
    assert _orphan_verdict({"AF_SERVICE_ID": "com.af.codex-transcript-cleanup"}) == "service"
    assert _orphan_verdict({"AF_SERVICE_ID": "com.opendj.claude-helper"}) == "service"
    # the control: a Claude session's own profile id IS agent provenance
    assert _orphan_verdict({"AF_SERVICE_ID": "com.af.claude-profiles.account3"}) == "reapable"


def test_an_orphan_whose_environment_cannot_be_read_is_never_reapable() -> None:
    """macOS hides a retitled process's env from `ps -E`: its own service id is
    then unknown, so the census must fail closed instead of trusting heuristics."""
    assert _orphan_verdict({}, env_readable=False) == "unreadable-orphan"
    assert _orphan_verdict({}, env_readable=True) == "reapable"  # control: same tree, env read


def test_a_subreaper_adoptee_is_orphaned_and_a_tmux_child_is_not() -> None:
    """Pure classification: a `systemd --user` subreaper that adopted a test
    server is ABOVE the tree, never its session host (and never its supervisor)."""
    test_env = {"AF_SERVICE_ID": "com.opendj.test.adopted"}
    procs = {
        1: _proc(1, 0, "/sbin/init"),
        10: _proc(10, 1, "/usr/lib/systemd/systemd --user", supervisor="job"),
        20: _proc(20, 10, "uvicorn apps.engine_core", env=dict(test_env)),
        30: _proc(30, 1, "tmux new-session -d"),
        40: _proc(40, 30, "uvicorn apps.engine_core", env=dict(test_env)),
    }
    rows = {r.pid: r for r in classify(procs)}
    assert (rows[20].tree, rows[20].verdict) == ("orphaned", "reapable"), rows[20]
    assert (rows[40].tree, rows[40].verdict) == ("session", "active"), rows[40]


def test_reap_reports_a_target_that_vanished_before_its_signal_as_not_killed() -> None:
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    procs = {gone.pid: _proc(gone.pid, 1, "pytest leftover", env={"CLAUDECODE": "1"})}
    rows = [
        Row(
            pid=gone.pid,
            ppid=1,
            pgid=gone.pid,
            root_pid=gone.pid,
            root_command="pytest leftover",
            age_s=3600,
            state="S",
            cwd="/",
            command="pytest leftover",
            env={"CLAUDECODE": "1"},
            env_markers=[],
            tree="orphaned",
            attribution="agent",
            verdict="reapable",
        )
    ]
    report = reap(procs, rows, min_age_s=0, dry_run=False)
    assert [k["outcome"] for k in report["kills"]] == ["gone-before-signal"]
    assert report["killed"] == 0
