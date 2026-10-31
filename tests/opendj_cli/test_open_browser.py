"""UIURL-02: `opendj open browser` opens the engine's own ui_url, or refuses.

[if] the engine serves a UI [then] the CLI hands its named URL to the browser, [else stop].

Everything here is a real process configured the way production configures
it: the packaged engine (`apps.engine_core serve`) is started as a subprocess
with its bundle directory set through `MDT_FRONTEND_BUILD_DIR`, and the CLI
runs as a second subprocess against that engine's real lock file. The
browser is the standard library's own `BROWSER` setting, the documented way
`webbrowser` is told which program to launch; here that program records the
argv it received, so the test reads back the URL a browser was actually
handed. No in-process patching.
"""
from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from apps.opendj_cli import EXIT_CONFIRMED, EXIT_FAILED

pytestmark = pytest.mark.requirement("UIURL-02")

REPO = Path(__file__).resolve().parents[2]
RECORDER = "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8')"
BOOT_DEADLINE_S = 60.0


@dataclass(frozen=True)
class RunningEngine:
    port: int
    lock_path: Path


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _serve(tmp_path: Path, bundle_dir: Path, extra_env: dict[str, str]) -> Iterator[RunningEngine]:
    port = _free_port()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    log = tmp_path / "engine.log"
    env = {**os.environ, "MDT_LIBRARY_MODE": "local", "MDT_FRONTEND_BUILD_DIR": str(bundle_dir), **extra_env}
    with log.open("w", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            [sys.executable, "-m", "apps.engine_core", "serve", "--data-dir", str(data_dir), "--port", str(port)],
            cwd=REPO,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + BOOT_DEADLINE_S
            while True:
                assert proc.poll() is None, log.read_text(encoding="utf-8")[-2000:]
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=1)
                    break
                except OSError:
                    assert time.monotonic() < deadline, "the engine never answered /api/v1/health"
                    time.sleep(0.2)
            yield RunningEngine(port=port, lock_path=data_dir / ".engine.lock")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


@pytest.fixture
def engine_with_bundle(tmp_path: Path) -> Iterator[RunningEngine]:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "index.html").write_text("<!doctype html><title>Open DJ</title>", encoding="utf-8")
    yield from _serve(tmp_path, bundle, {})


@pytest.fixture
def engine_without_ui(tmp_path: Path) -> Iterator[RunningEngine]:
    # An empty bundle dir and no paired frontend: the engine serves API only.
    bundle = tmp_path / "empty-bundle"
    bundle.mkdir()
    yield from _serve(tmp_path, bundle, {"MUSIC_DJ_FRONTEND_PORT": ""})


def _browser_command_template(executable: str, recorder: str | Path, received: str | Path) -> str:
    return f"{shlex.quote(str(executable))} {shlex.quote(str(recorder))} {shlex.quote(str(received))} %s"


def _open_browser(engine: RunningEngine, tmp_path: Path) -> tuple[subprocess.CompletedProcess[str], Path]:
    received = tmp_path / "browser-received.txt"
    recorder = tmp_path / "recorder.py"
    recorder.write_text(RECORDER, encoding="utf-8")
    env = {**os.environ, "BROWSER": _browser_command_template(sys.executable, recorder, received)}
    result = subprocess.run(
        [sys.executable, "-m", "apps.opendj_cli", "--json", "--lock", str(engine.lock_path), "open", "browser"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return result, received


def test_open_browser_hands_the_engine_named_url_to_the_browser(
    engine_with_bundle: RunningEngine, tmp_path: Path
) -> None:
    result, received = _open_browser(engine_with_bundle, tmp_path)
    assert result.returncode == EXIT_CONFIRMED, result.stderr
    expected = f"http://opendj.localhost:{engine_with_bundle.port}/"
    assert received.read_text(encoding="utf-8") == expected
    body = json.loads(result.stdout)
    assert (body["ui_url"], body["served_by"], body["opened"]) == (expected, "engine", True)


def test_the_named_url_reaches_the_engine_it_names(engine_with_bundle: RunningEngine) -> None:
    # The address is only useful if the engine answers it: Host opendj.localhost.
    request = urllib.request.Request(
        f"http://127.0.0.1:{engine_with_bundle.port}/api/v1/ui-url",
        headers={"Host": f"opendj.localhost:{engine_with_bundle.port}"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        body = json.load(response)
    assert body["ui_url"] == f"http://opendj.localhost:{engine_with_bundle.port}/"


def test_open_browser_refuses_when_the_engine_serves_no_ui(
    engine_without_ui: RunningEngine, tmp_path: Path
) -> None:
    result, received = _open_browser(engine_without_ui, tmp_path)
    assert result.returncode == EXIT_FAILED
    assert "UI_NOT_SERVED" in result.stdout + result.stderr
    assert not received.exists(), "a browser was launched for an engine with no UI"
    with urllib.request.urlopen(f"http://127.0.0.1:{engine_without_ui.port}/api/v1/health", timeout=5) as response:
        assert json.load(response)["ui_url"] is None


def test_browser_command_template_quotes_windows_paths_for_webbrowser() -> None:
    executable = r"C:\Program Files\Python\python.exe"
    recorder = r"C:\Users\dev\AppData\Local\Temp\My Recorder\recorder.py"
    received = r"C:\Users\dev\AppData\Local\Temp\My Recorder\browser-received.txt"
    template = _browser_command_template(executable, recorder, received)
    assert shlex.quote(executable) in template
    assert shlex.quote(recorder) in template
    assert shlex.quote(received) in template
    expected = [executable, recorder, received, "%s"]
    assert shlex.split(template) == expected
    browser = webbrowser.get(template)
    assert browser.name == executable
    assert browser.args == [recorder, received, "%s"]
    unquoted = f"{executable} {recorder} {received} %s"
    assert shlex.split(unquoted) != expected
