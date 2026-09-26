"""Nightly job owns a throwaway scratch engine (issue #2112)."""

from __future__ import annotations

import contextlib
import http.server
import json
import plistlib
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from apps.webui.frontend.tests.e2e.support.deckload_fixture import build
from scripts.perf import perf_kpi_job, perf_kpi_ledger_pr
from scripts.perf.kpi_ledger_append import append_entries
from scripts.perf.perf_kpi_config import REPO_ROOT, load_config, require_machine_label
from scripts.perf.perf_kpi_job import cmd_nightly
from scripts.perf.perf_kpi_nightly import ENGINE_READY_TIMEOUT_S, SCRATCH_ENGINE_LOG_NAME
from tests.perf.ledger_pr_fixtures import (
    init_upstream_and_clone,
    install_gh,
    ledger_entry,
    run_git,
)

INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install_perf_kpi_launchd.sh"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _empty_ledger(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 2, "entries": []}, indent=2) + "\n",
        encoding="utf-8",
    )


def _nightly_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **overrides: str) -> Path:
    state_dir = tmp_path / "state"
    ledger = tmp_path / "kpi-ledger.json"
    _empty_ledger(ledger)
    values = {
        "MDT_PERF_KPI_STATE_DIR": str(state_dir),
        "MDT_PERF_KPI_LEDGER": str(ledger),
        "MDT_PERF_KPI_SCRATCH_PORT": str(_free_port()),
        "MDT_PERF_KPI_SAMPLES": "2",
        "MDT_PERF_KPI_MACHINE": "test",
        **overrides,
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return state_dir


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


def test_nightly_fails_when_data_dir_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If MDT_PERF_KPI_DATA_DIR is unset then nightly exits non-zero with engine_unavailable."""
    state_dir = _nightly_env(monkeypatch, tmp_path)
    monkeypatch.delenv("MDT_PERF_KPI_DATA_DIR", raising=False)
    config = load_config()

    code = cmd_nightly(config, base_url=None, skip_pr=True)

    assert code != 0
    assert "engine_unavailable" in _history_events(state_dir)
    assert _ledger_entries(tmp_path) == []


def test_nightly_fails_when_state_db_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If state/state.db is missing then nightly exits non-zero with engine_unavailable."""
    data_dir = tmp_path / "library"
    data_dir.mkdir()
    state_dir = _nightly_env(
        monkeypatch,
        tmp_path,
        MDT_PERF_KPI_DATA_DIR=str(data_dir),
    )
    config = load_config()
    missing = data_dir / "state" / "state.db"

    code = cmd_nightly(config, base_url=None, skip_pr=True)

    assert code != 0
    assert "engine_unavailable" in _history_events(state_dir)
    assert _ledger_entries(tmp_path) == []
    history = (state_dir / "history.jsonl").read_text(encoding="utf-8")
    assert str(missing) in history


def test_nightly_refuses_bound_scratch_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the scratch port is already bound then nightly exits without touching the occupant."""
    port = _free_port()
    data_dir = tmp_path / "library"
    (data_dir / "state").mkdir(parents=True)
    (data_dir / "state" / "state.db").touch()
    state_dir = _nightly_env(
        monkeypatch,
        tmp_path,
        MDT_PERF_KPI_DATA_DIR=str(data_dir),
        MDT_PERF_KPI_SCRATCH_PORT=str(port),
    )
    occupant = _occupy(port)
    try:
        config = load_config()
        code = cmd_nightly(config, base_url=None, skip_pr=True)
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
    port = _free_port()
    data_dir = tmp_path / "fixture-data"
    build(data_dir)
    state_dir = _nightly_env(
        monkeypatch,
        tmp_path,
        MDT_PERF_KPI_DATA_DIR=str(data_dir),
        MDT_PERF_KPI_SCRATCH_PORT=str(port),
    )
    monkeypatch.delenv("MDT_PERF_KPI_SMALL_STABLE_ID", raising=False)
    monkeypatch.delenv("MDT_PERF_KPI_LARGE_STABLE_ID", raising=False)
    monkeypatch.delenv("MDT_PERF_KPI_STEMMED_STABLE_ID", raising=False)
    config = load_config()

    code = cmd_nightly(config, base_url=None, skip_pr=True)

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
    port = _free_port()
    data_dir = tmp_path / "library"
    (data_dir / "state").mkdir(parents=True)
    with sqlite3.connect(data_dir / "state" / "state.db") as connection:
        connection.execute("CREATE TABLE tracks (stable_id TEXT PRIMARY KEY, title TEXT)")
    (data_dir / "progress-tree.yaml").write_text("nodes: []\n", encoding="utf-8")
    state_dir = _nightly_env(
        monkeypatch,
        tmp_path,
        MDT_PERF_KPI_DATA_DIR=str(data_dir),
        MDT_PERF_KPI_SCRATCH_PORT=str(port),
    )
    config = load_config()

    code = cmd_nightly(config, base_url=None, skip_pr=True)
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
    port = _free_port()
    data_dir = tmp_path / "library"
    (data_dir / "state").mkdir(parents=True)
    (data_dir / "state" / "state.db").touch()
    (data_dir / "progress-tree.yaml").write_text("nodes: []\n", encoding="utf-8")
    state_dir = _nightly_env(
        monkeypatch,
        tmp_path,
        MDT_PERF_KPI_DATA_DIR=str(data_dir),
        MDT_PERF_KPI_SCRATCH_PORT=str(port),
    )
    config = load_config()
    started = time.monotonic()

    code = cmd_nightly(config, base_url=None, skip_pr=True)
    elapsed = time.monotonic() - started
    captured = capsys.readouterr()
    log_path = state_dir / SCRATCH_ENGINE_LOG_NAME

    assert code != 0
    assert elapsed < ENGINE_READY_TIMEOUT_S / 2
    assert str(log_path) in captured.err or str(log_path) in captured.out
    assert "engine_unavailable" in _history_events(state_dir)
    assert _ledger_entries(tmp_path) == []
    assert not _listening(port)


def test_base_url_starts_no_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If --base-url is passed then no engine is started and DATA_DIR is not required."""
    scratch_port = _free_port()
    closed_port = _free_port()
    data_dir = tmp_path / "library"
    (data_dir / "state").mkdir(parents=True)
    (data_dir / "state" / "state.db").touch()
    state_dir = _nightly_env(
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

    nightly = plistlib.loads(
        (launch_agents / "com.af.perf-kpi-nightly.plist").read_bytes()
    )
    health = plistlib.loads(
        (launch_agents / "com.af.perf-kpi-health.plist").read_bytes()
    )
    env = nightly["EnvironmentVariables"]
    assert env["MDT_PERF_KPI_DATA_DIR"] == "/abs/lib"
    assert ".local/bin" in env["PATH"] or ".venv/bin" in env["PATH"]
    assert "MDT_PERF_KPI_DATA_DIR" not in health.get("EnvironmentVariables", {})


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
    _nightly_env(monkeypatch, tmp_path)
    monkeypatch.delenv("MDT_PERF_KPI_MACHINE", raising=False)
    config = load_config()
    assert config.machine == ""


def test_require_machine_label_raises_when_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """cmd_nightly's own guard (require_machine_label) still fails loud
    before doing any nightly work when the label truly is missing."""
    _nightly_env(monkeypatch, tmp_path)
    monkeypatch.delenv("MDT_PERF_KPI_MACHINE", raising=False)
    config = load_config()
    with pytest.raises(ValueError, match="MDT_PERF_KPI_MACHINE"):
        require_machine_label(config)


# ----- measurement-window guard, real publish path (Codex, PR #3827, P1/BLOCKING) --
# A disposable upstream + clone stands in for origin/REPO_ROOT, and a real `gh`
# on PATH answers from captured exchanges (tests/perf/ledger_pr_fixtures.py), so
# `update_ledger_pr` runs for real: worktree, push, restore. The "engine" is a
# real local HTTP server that answers 503, so the nightly records error rows
# without a library. Concurrent writers are simulated by that server (an edit
# while the run is measuring) or by WRAPPING the real guard (an edit just after
# it validated); nothing in scripts/ is replaced.


class _EditingEngine(http.server.BaseHTTPRequestHandler):
    ledger: Path
    edit_on_first_request = False
    edited = False

    def do_GET(self) -> None:
        cls = type(self)
        if cls.edit_on_first_request and not cls.edited:
            append_entries(cls.ledger, [ledger_entry("operator-edit")])
            cls.edited = True
        self.send_response(503)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        return None


@contextlib.contextmanager
def _real_publish_setup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, edit_mid_run: bool
) -> Iterator[tuple[Path, Path, Path, str]]:
    upstream, repo_root = init_upstream_and_clone(tmp_path)
    install_gh(monkeypatch, tmp_path, open_pr=False)
    ledger = repo_root / "docs" / "perf" / "kpi-ledger.json"
    data_dir = tmp_path / "library"
    (data_dir / "state").mkdir(parents=True)
    (data_dir / "state" / "state.db").touch()
    _nightly_env(
        monkeypatch, tmp_path, MDT_PERF_KPI_DATA_DIR=str(data_dir), MDT_PERF_KPI_LEDGER=str(ledger)
    )
    monkeypatch.setattr(perf_kpi_job, "REPO_ROOT", repo_root)
    handler: type[_EditingEngine] = type(
        "_Engine", (_EditingEngine,), {"ledger": ledger, "edit_on_first_request": edit_mid_run}
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield upstream, repo_root, ledger, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _branch_notes(upstream: Path) -> list[str] | None:
    branch = perf_kpi_ledger_pr.LEDGER_PR_BRANCH
    if branch not in run_git("branch", "--list", branch, cwd=upstream):
        return None
    shown = run_git("show", f"{branch}:docs/perf/kpi-ledger.json", cwd=upstream)
    return [row.get("note") for row in json.loads(shown)["entries"]]


def test_nightly_publishes_and_restores_when_nobody_touches_the_ledger(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Control: an undisturbed run publishes its rows and leaves REPO_ROOT clean."""
    setup = _real_publish_setup(monkeypatch, tmp_path, edit_mid_run=False)
    with setup as (upstream, repo_root, _ledger, base_url):
        cmd_nightly(load_config(), base_url=base_url, skip_pr=False)
    published = _branch_notes(upstream)
    assert published is not None and published, "the nightly rows never reached the branch"
    assert "operator-edit" not in published
    assert run_git("status", "--porcelain", cwd=repo_root) == ""


def test_nightly_refuses_to_publish_a_ledger_edited_mid_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[if] the ledger is edited while the nightly is measuring [then] nothing
    is published or restored and the edit stays in the file, [else stop]."""
    setup = _real_publish_setup(monkeypatch, tmp_path, edit_mid_run=True)
    busy = pytest.raises(RuntimeError, match="changed while the nightly run was measuring")
    with setup as (upstream, _repo_root, ledger, base_url), busy:
        cmd_nightly(load_config(), base_url=base_url, skip_pr=False)
    assert _branch_notes(upstream) is None
    notes = [row.get("note") for row in json.loads(ledger.read_text(encoding="utf-8"))["entries"]]
    assert "operator-edit" in notes


def test_an_edit_after_validation_is_neither_published_nor_erased(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """[if] the ledger is edited after the guard validated it but before the
    publish reads it [then] only the validated rows are published and the
    restore refuses, keeping the edit (review comment 4109856184), [else stop]."""
    setup = _real_publish_setup(monkeypatch, tmp_path, edit_mid_run=False)
    real_guard = perf_kpi_job.refuse_ledger_edits_made_during_run

    def _guard_then_concurrent_edit(*args: Any, **kwargs: Any) -> str:
        validated = real_guard(*args, **kwargs)
        append_entries(ledger, [ledger_entry("operator-edit")])
        return validated

    monkeypatch.setattr(
        perf_kpi_job, "refuse_ledger_edits_made_during_run", _guard_then_concurrent_edit
    )
    refused = pytest.raises(RuntimeError, match="changed while the ledger publish was running")
    with setup as (upstream, _repo_root, ledger, base_url), refused:
        cmd_nightly(load_config(), base_url=base_url, skip_pr=False)
    published = _branch_notes(upstream)
    assert published is not None and published
    assert "operator-edit" not in published
    notes = [row.get("note") for row in json.loads(ledger.read_text(encoding="utf-8"))["entries"]]
    assert "operator-edit" in notes
