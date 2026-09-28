"""Nightly job owns a throwaway scratch engine (issue #2112)."""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from apps.webui.frontend.tests.e2e.support.deckload_fixture import build
from scripts.perf.perf_kpi_config import REPO_ROOT, load_config, require_machine_label
from scripts.perf.perf_kpi_job import cmd_nightly
from scripts.perf.perf_kpi_nightly import ENGINE_READY_TIMEOUT_S, SCRATCH_ENGINE_LOG_NAME
from tests.perf.perf_kpi_job_fixtures import free_port, nightly_env

INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install_perf_kpi_launchd.sh"
DECIDE_SCRIPT = REPO_ROOT / "scripts" / "perf_kpi_launchd_decide.sh"
LAUNCHCTL_UNAVAILABLE = sys.platform != "darwin" or shutil.which("launchctl") is None


def _history_events(state_dir: Path) -> list[str]:
    history = state_dir / "history.jsonl"
    if not history.exists():
        return []
    return [
        json.loads(line)["event"]
        for line in history.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


def _occupy(port: int) -> subprocess.Popen[bytes]:
    code = (
        "import http.server, sys\n"
        "http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])),"
        " http.server.SimpleHTTPRequestHandler).serve_forever()\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        if _listening(port):
            return proc
        time.sleep(0.1)
    proc.kill()
    raise AssertionError(f"occupant never bound {port}")


def _ledger_entries(tmp_path: Path) -> list[dict]:
    ledger = json.loads((tmp_path / "kpi-ledger.json").read_text(encoding="utf-8"))
    return ledger["entries"]


def test_nightly_fails_when_data_dir_unset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """If MDT_PERF_KPI_DATA_DIR is unset then nightly exits non-zero with engine_unavailable."""
    state_dir = nightly_env(monkeypatch, tmp_path)
    monkeypatch.delenv("MDT_PERF_KPI_DATA_DIR", raising=False)
    config = load_config()

    code = cmd_nightly(config, repo_root=REPO_ROOT, base_url=None, skip_pr=True)

    assert code != 0
    assert "engine_unavailable" in _history_events(state_dir)
    assert _ledger_entries(tmp_path) == []


def test_nightly_fails_when_state_db_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If state/state.db is missing then nightly exits non-zero with engine_unavailable."""
    data_dir = tmp_path / "library"
    data_dir.mkdir()
    state_dir = nightly_env(
        monkeypatch,
        tmp_path,
        MDT_PERF_KPI_DATA_DIR=str(data_dir),
    )
    config = load_config()
    missing = data_dir / "state" / "state.db"

    code = cmd_nightly(config, repo_root=REPO_ROOT, base_url=None, skip_pr=True)

    assert code != 0
    assert "engine_unavailable" in _history_events(state_dir)
    assert _ledger_entries(tmp_path) == []
    history = (state_dir / "history.jsonl").read_text(encoding="utf-8")
    assert str(missing) in history


def test_nightly_refuses_bound_scratch_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the scratch port is already bound then nightly exits without touching the occupant."""
    port = free_port()
    data_dir = tmp_path / "library"
    (data_dir / "state").mkdir(parents=True)
    (data_dir / "state" / "state.db").touch()
    state_dir = nightly_env(
        monkeypatch,
        tmp_path,
        MDT_PERF_KPI_DATA_DIR=str(data_dir),
        MDT_PERF_KPI_SCRATCH_PORT=str(port),
    )
    occupant = _occupy(port)
    try:
        config = load_config()
        code = cmd_nightly(config, repo_root=REPO_ROOT, base_url=None, skip_pr=True)
        assert code != 0
        assert occupant.poll() is None
        assert _listening(port)
        assert _ledger_entries(tmp_path) == []
        assert "engine_unavailable" in _history_events(state_dir)
    finally:
        occupant.terminate()
        occupant.wait(timeout=5)


def test_nightly_starts_real_engine_and_stops_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If a real fixture library is available then nightly runs and releases the scratch port."""
    port = free_port()
    data_dir = tmp_path / "fixture-data"
    build(data_dir)
    state_dir = nightly_env(
        monkeypatch,
        tmp_path,
        MDT_PERF_KPI_DATA_DIR=str(data_dir),
        MDT_PERF_KPI_SCRATCH_PORT=str(port),
    )
    monkeypatch.delenv("MDT_PERF_KPI_SMALL_STABLE_ID", raising=False)
    monkeypatch.delenv("MDT_PERF_KPI_LARGE_STABLE_ID", raising=False)
    monkeypatch.delenv("MDT_PERF_KPI_STEMMED_STABLE_ID", raising=False)
    config = load_config()

    code = cmd_nightly(config, repo_root=REPO_ROOT, base_url=None, skip_pr=True)

    assert code != 1
    entries = _ledger_entries(tmp_path)
    assert entries
    assert any(row.get("kpi") == "packaged_deck_load_total_ms" for row in entries)
    assert not _listening(port)
    assert (state_dir / SCRATCH_ENGINE_LOG_NAME).is_file()


def test_nightly_refuses_a_library_with_no_tracks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """[if] state.db has an empty tracks table [then] nightly refuses before boot, [else stop]."""
    port = free_port()
    data_dir = tmp_path / "library"
    (data_dir / "state").mkdir(parents=True)
    with sqlite3.connect(data_dir / "state" / "state.db") as connection:
        connection.execute("CREATE TABLE tracks (stable_id TEXT PRIMARY KEY, title TEXT)")
    (data_dir / "progress-tree.yaml").write_text("nodes: []\n", encoding="utf-8")
    state_dir = nightly_env(
        monkeypatch,
        tmp_path,
        MDT_PERF_KPI_DATA_DIR=str(data_dir),
        MDT_PERF_KPI_SCRATCH_PORT=str(port),
    )
    config = load_config()

    code = cmd_nightly(config, repo_root=REPO_ROOT, base_url=None, skip_pr=True)
    captured = capsys.readouterr()

    assert code != 0
    assert "has no tracks" in captured.err
    assert "engine_unavailable" in _history_events(state_dir)
    assert _ledger_entries(tmp_path) == []
    assert not _listening(port)


def test_nightly_fails_fast_when_engine_exits_before_healthy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """If the engine exits before healthy then nightly fails fast and prints the log path."""
    port = free_port()
    data_dir = tmp_path / "library"
    (data_dir / "state").mkdir(parents=True)
    (data_dir / "state" / "state.db").touch()
    (data_dir / "progress-tree.yaml").write_text("nodes: []\n", encoding="utf-8")
    state_dir = nightly_env(
        monkeypatch,
        tmp_path,
        MDT_PERF_KPI_DATA_DIR=str(data_dir),
        MDT_PERF_KPI_SCRATCH_PORT=str(port),
    )
    config = load_config()
    started = time.monotonic()

    code = cmd_nightly(config, repo_root=REPO_ROOT, base_url=None, skip_pr=True)
    elapsed = time.monotonic() - started
    captured = capsys.readouterr()
    log_path = state_dir / SCRATCH_ENGINE_LOG_NAME

    assert code != 0
    assert elapsed < ENGINE_READY_TIMEOUT_S / 2
    assert str(log_path) in captured.err or str(log_path) in captured.out
    assert "engine_unavailable" in _history_events(state_dir)
    assert _ledger_entries(tmp_path) == []
    assert not _listening(port)


def test_base_url_starts_no_engine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """If --base-url is passed then no engine is started and DATA_DIR is not required."""
    scratch_port = free_port()
    closed_port = free_port()
    data_dir = tmp_path / "library"
    (data_dir / "state").mkdir(parents=True)
    (data_dir / "state" / "state.db").touch()
    state_dir = nightly_env(
        monkeypatch,
        tmp_path,
        MDT_PERF_KPI_DATA_DIR=str(data_dir),
        MDT_PERF_KPI_SCRATCH_PORT=str(scratch_port),
    )
    occupant = _occupy(scratch_port)
    try:
        config = load_config()
        code = cmd_nightly(
            config,
            repo_root=REPO_ROOT,
            base_url=f"http://127.0.0.1:{closed_port}",
            skip_pr=True,
        )
        assert occupant.poll() is None
        assert _listening(scratch_port)
        assert _ledger_entries(tmp_path)
        assert "engine_unavailable" not in _history_events(state_dir)
        assert not (state_dir / SCRATCH_ENGINE_LOG_NAME).exists()
        assert code != 1
    finally:
        occupant.terminate()
        occupant.wait(timeout=5)


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


def test_load_config_does_not_require_machine_label(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The health path never attributes a ledger entry to a machine (round 4,
    P1/BLOCKING): Air's already-installed com.af.perf-kpi-health plist
    predates MDT_PERF_KPI_MACHINE entirely, so requiring it inside
    load_config() itself -- which both cmd_health and cmd_nightly call --
    would break Air's wedged-engine detection on merge with no reinstall
    step. load_config() must succeed with machine=="" when the env var is
    unset; only the nightly path enforces it."""
    nightly_env(monkeypatch, tmp_path)
    monkeypatch.delenv("MDT_PERF_KPI_MACHINE", raising=False)
    config = load_config()
    assert config.machine == ""


def test_require_machine_label_raises_when_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """cmd_nightly's own guard (require_machine_label) still fails loud
    before doing any nightly work when the label truly is missing."""
    nightly_env(monkeypatch, tmp_path)
    monkeypatch.delenv("MDT_PERF_KPI_MACHINE", raising=False)
    config = load_config()
    with pytest.raises(ValueError, match="MDT_PERF_KPI_MACHINE"):
        require_machine_label(config)
