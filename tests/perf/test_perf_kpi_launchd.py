"""Perf KPI launchd install and health-agent cleanup tests (split from test_perf_kpi_job.py)."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from scripts.perf.perf_kpi_config import REPO_ROOT

INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install_perf_kpi_launchd.sh"
DECIDE_SCRIPT = REPO_ROOT / "scripts" / "perf_kpi_launchd_decide.sh"
LAUNCHCTL_UNAVAILABLE = sys.platform != "darwin" or shutil.which("launchctl") is None


def test_install_render_includes_data_dir_and_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the launchd installer renders plists then nightly gets DATA_DIR and a uv-capable PATH."""
    home = tmp_path / "home"
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MDT_PERF_KPI_SMALL_STABLE_ID", "sid-small")
    monkeypatch.setenv("MDT_PERF_KPI_LARGE_STABLE_ID", "sid-large")
    monkeypatch.setenv("MDT_PERF_KPI_STEMMED_STABLE_ID", "sid-stemmed")
    monkeypatch.setenv("MDT_PERF_KPI_DATA_DIR", "/abs/lib")
    monkeypatch.setenv("MDT_PERF_KPI_MACHINE", "air")

    completed = subprocess.run(
        [str(INSTALL_SCRIPT)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0

    nightly = plistlib.loads((launch_agents / "com.af.perf-kpi-nightly.plist").read_bytes())
    health = plistlib.loads((launch_agents / "com.af.perf-kpi-health.plist").read_bytes())
    env = nightly["EnvironmentVariables"]
    assert env["MDT_PERF_KPI_DATA_DIR"] == "/abs/lib"
    assert ".local/bin" in env["PATH"] or ".venv/bin" in env["PATH"]
    assert "MDT_PERF_KPI_DATA_DIR" not in health.get("EnvironmentVariables", {})


def test_install_nightly_only_skips_health(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """--nightly-only renders and would bootstrap the nightly agent only, for a host
    (e.g. demon-llama) with no live-review-preview service for the health leg to probe."""
    home = tmp_path / "home"
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MDT_PERF_KPI_SMALL_STABLE_ID", "sid-small")
    monkeypatch.setenv("MDT_PERF_KPI_LARGE_STABLE_ID", "sid-large")
    monkeypatch.setenv("MDT_PERF_KPI_STEMMED_STABLE_ID", "sid-stemmed")
    monkeypatch.setenv("MDT_PERF_KPI_DATA_DIR", "/abs/lib")
    monkeypatch.setenv("MDT_PERF_KPI_MACHINE", "demon-llama")

    completed = subprocess.run(
        [str(INSTALL_SCRIPT), "--nightly-only"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert (launch_agents / "com.af.perf-kpi-nightly.plist").exists()
    assert not (launch_agents / "com.af.perf-kpi-health.plist").exists()


def test_install_nightly_only_render_removes_a_stale_health_plist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[if] --nightly-only runs render-only (no --install) over a health plist left by an
    earlier render [then] that plist is removed, so it cannot reload the unwanted health
    agent at the next login (sol-review #4079 P2); launchctl is not touched."""
    home = tmp_path / "home"
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    stale = launch_agents / "com.af.perf-kpi-health.plist"
    stale.write_text("<plist/>")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MDT_PERF_KPI_SMALL_STABLE_ID", "sid-small")
    monkeypatch.setenv("MDT_PERF_KPI_LARGE_STABLE_ID", "sid-large")
    monkeypatch.setenv("MDT_PERF_KPI_STEMMED_STABLE_ID", "sid-stemmed")
    monkeypatch.setenv("MDT_PERF_KPI_DATA_DIR", "/abs/lib")
    monkeypatch.setenv("MDT_PERF_KPI_MACHINE", "demon-llama")

    completed = subprocess.run(
        [str(INSTALL_SCRIPT), "--nightly-only"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert (launch_agents / "com.af.perf-kpi-nightly.plist").exists()
    assert not stale.exists(), "render-only --nightly-only left the stale health plist"


def test_remove_stale_health_plist_fails_when_the_plist_cannot_be_removed(tmp_path: Path) -> None:
    """[if] the stale health plist cannot be deleted [then] remove_stale_health_plist returns
    non-zero, even under `set -e` with `|| exit 1` (which disables set -e inside the function).
    Calls the helper directly, so it runs on every host, launchctl or not (sol-review #4079 P1)."""
    locked = tmp_path / "locked"
    locked.mkdir()
    plist = locked / "com.af.perf-kpi-health.plist"
    plist.write_text("<plist/>")
    locked.chmod(0o555)
    try:
        completed = subprocess.run(
            [
                "bash",
                "-c",
                f'set -e; source "{DECIDE_SCRIPT}"; '
                'remove_stale_health_plist "$1" || exit 1; echo REACHED',
                "_",
                str(plist),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        locked.chmod(0o755)

    assert plist.exists(), "control: the locked directory must really block deletion"
    assert completed.returncode != 0, completed.stdout
    assert "REACHED" not in completed.stdout


def test_remove_stale_health_plist_removes_a_present_plist_and_tolerates_absence(
    tmp_path: Path,
) -> None:
    """[if] the plist exists and can be deleted [then] it is removed and the helper exits 0;
    [if] it is already absent [then] the helper exits 0 (control for the failure test above)."""
    plist = tmp_path / "com.af.perf-kpi-health.plist"
    plist.write_text("<plist/>")
    for _ in range(2):
        completed = subprocess.run(
            [
                "bash",
                "-c",
                f'source "{DECIDE_SCRIPT}" && remove_stale_health_plist "$1"',
                "_",
                str(plist),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
        assert not plist.exists()


@pytest.mark.skipif(
    LAUNCHCTL_UNAVAILABLE,
    reason=(
        "UNAVAILABLE: without launchctl, cleanup_stale_health_agent returns via its "
        "unknown branch before rm"
    ),
)
def test_cleanup_stale_health_agent_fails_when_the_plist_cannot_be_removed(tmp_path: Path) -> None:
    """[if] the stale health plist cannot be deleted [then] cleanup_stale_health_agent
    returns non-zero and does not report it removed, even when called as `... || exit 1`,
    which disables set -e inside the function (sol-review #4079 P1). The label is one no
    agent uses, so launchctl print reports it absent and only the rm step is exercised."""
    locked = tmp_path / "locked"
    locked.mkdir()
    plist = locked / "com.af.perf-kpi-health.plist"
    plist.write_text("<plist/>")
    locked.chmod(0o555)
    try:
        completed = subprocess.run(
            [
                "bash",
                "-c",
                f'set -e; source "{DECIDE_SCRIPT}"; '
                'cleanup_stale_health_agent "$1" "$2" "$3" || exit 1; echo REACHED',
                "_",
                str(os.getuid()),
                "com.af.perf-kpi-health-absent-probe",
                str(plist),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        locked.chmod(0o755)

    assert plist.exists(), "control: the locked directory must really block deletion"
    assert completed.returncode != 0, completed.stdout
    assert "REACHED" not in completed.stdout
    assert "[OK] removed" not in completed.stdout


def _decide(print_rc: int) -> str:
    """Call the real health_agent_action (scripts/perf_kpi_launchd_decide.sh)
    by sourcing it and invoking the function -- no PATH tricks, no fake
    launchctl, just the shipped bash function asked one question."""
    completed = subprocess.run(
        ["bash", "-c", f'source "{DECIDE_SCRIPT}" && health_agent_action "$1"', "_", str(print_rc)],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def test_health_agent_action_bootout_when_print_exits_zero() -> None:
    """[if] launchctl print exits 0 [then] health_agent_action returns bootout, [else stop]."""
    assert _decide(0) == "bootout"


def test_health_agent_action_absent_when_print_exits_113() -> None:
    """[if] launchctl print exits 113 [then] health_agent_action returns absent, [else stop]."""
    assert _decide(113) == "absent"


@pytest.mark.parametrize("print_rc", [5, 1])
def test_health_agent_action_unknown_for_any_other_exit_code(print_rc: int) -> None:
    """[if] launchctl print exits anything but 0 or 113 [then] health_agent_action
    returns unknown, [else stop]."""
    assert _decide(print_rc) == "unknown"


@pytest.mark.skipif(
    LAUNCHCTL_UNAVAILABLE,
    reason="UNAVAILABLE: launchctl is a macOS-only binary, not present on this host",
)
def test_cleanup_stale_health_agent_boots_out_a_real_throwaway_agent(
    tmp_path: Path,
) -> None:
    """[if] a uniquely-labelled real launchd agent is loaded [then]
    cleanup_stale_health_agent (the exact function install_perf_kpi_launchd.sh's
    --nightly-only path calls) boots it out via real launchctl and removes its
    plist, proven by launchctl print going from exit 0 to exit 113, [else stop].

    Drives cleanup_stale_health_agent directly rather than the installer script:
    the installer's own bootstrap loop unconditionally touches the real
    com.af.perf-kpi-nightly label regardless of --nightly-only, so it is never
    safe to invoke against a throwaway target. This is the same real
    print/bootout/loaded-state code the installer runs, just called without the
    unrelated, unsafe-to-fake bootstrap loop around it. Never touches any real
    com.af.perf-kpi-* label; the plist bootstrapped here carries a fresh
    com.af.test-perf-kpi-<uuid> Label of its own.
    """
    uid = os.getuid()
    label = f"com.af.test-perf-kpi-{uuid.uuid4().hex[:8]}"
    plist_path = tmp_path / f"{label}.plist"
    plist_path.write_bytes(
        plistlib.dumps(
            {
                "Label": label,
                "ProgramArguments": ["/usr/bin/true"],
                "RunAtLoad": False,
                "KeepAlive": False,
            }
        )
    )

    def _print_rc() -> int:
        return subprocess.run(
            ["launchctl", "print", f"gui/{uid}/{label}"],
            check=False,
            capture_output=True,
        ).returncode

    subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
        check=True,
        capture_output=True,
    )
    try:
        assert _print_rc() == 0, "real bootstrap of the throwaway agent did not load it"

        completed = subprocess.run(
            [
                "bash",
                "-c",
                f'source "{DECIDE_SCRIPT}" && cleanup_stale_health_agent "$1" "$2" "$3"',
                "_",
                str(uid),
                label,
                str(plist_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr

        assert _print_rc() == 113, "cleanup_stale_health_agent did not really unload the agent"
        assert not plist_path.exists()
    finally:
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}/{label}"],
            check=False,
            capture_output=True,
        )


def test_install_requires_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """If MDT_PERF_KPI_DATA_DIR is unset then the installer exits non-zero."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MDT_PERF_KPI_SMALL_STABLE_ID", "sid-small")
    monkeypatch.setenv("MDT_PERF_KPI_LARGE_STABLE_ID", "sid-large")
    monkeypatch.setenv("MDT_PERF_KPI_STEMMED_STABLE_ID", "sid-stemmed")
    monkeypatch.delenv("MDT_PERF_KPI_DATA_DIR", raising=False)

    completed = subprocess.run(
        [str(INSTALL_SCRIPT)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2


def test_install_requires_host_label(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No hidden 'air' default (claude-review, PR #3827, round 3, P1/BLOCKING):
    an install without --host-label or MDT_PERF_KPI_MACHINE must refuse
    rather than silently attributing a second Mac's runs to Air."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MDT_PERF_KPI_SMALL_STABLE_ID", "sid-small")
    monkeypatch.setenv("MDT_PERF_KPI_LARGE_STABLE_ID", "sid-large")
    monkeypatch.setenv("MDT_PERF_KPI_STEMMED_STABLE_ID", "sid-stemmed")
    monkeypatch.setenv("MDT_PERF_KPI_DATA_DIR", "/abs/lib")
    monkeypatch.delenv("MDT_PERF_KPI_MACHINE", raising=False)

    completed = subprocess.run(
        [str(INSTALL_SCRIPT)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "--host-label" in completed.stderr
