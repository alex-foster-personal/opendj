"""Nightly job owns a throwaway scratch engine (issue #2112)."""

from __future__ import annotations

import json
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
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
