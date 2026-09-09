"""Real-process regression coverage for the E2E stale-server cleanup.

Requirements:

- ✔︎ A listener inside the assigned runner lane is terminated before Playwright claims ports.
- ✔︎ The cleanup log names the lane, port, PID, command, and working directory.

Acceptance tests:

- [if] a leaked apps.webui.server process listens in an E2E lane [then ⛔️]
  the next job reaches Playwright with that listener still alive.
- [if] cleanup terminates a stale process [then ⛔️] its lane, port, PID,
  command, and cwd are absent from the log.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HYGIENE = REPO_ROOT / "scripts" / "ci_e2e_port_hygiene.sh"
LANE = 50
BACKEND_PORT = 8680 + LANE * 840


@pytest.mark.skipif(shutil.which("lsof") is None, reason="lsof unavailable")
def test_hygiene_terminates_and_logs_a_real_stale_lane_listener(tmp_path: Path) -> None:
    """A real leaked engine listener is killed before the next pair claim."""
    stale = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import socket, time; "
                f"listener = socket.socket(); listener.bind(('127.0.0.1', {BACKEND_PORT})); "
                "listener.listen(); time.sleep(60)"
            ),
            "apps.webui.server",
        ],
        cwd=tmp_path,
        text=True,
    )
    try:
        time.sleep(0.1)
        result = subprocess.run(
            ["bash", str(HYGIENE)],
            env={
                "GITHUB_WORKSPACE": str(tmp_path),
                "MUSIC_DJ_PORT_LANE": str(LANE),
                "PATH": str(Path("/usr/bin")),
            },
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        assert stale.wait(timeout=2) is not None
        assert f"[e2e-hygiene] lane={LANE}" in result.stdout
        assert f"port={BACKEND_PORT}" in result.stdout
        assert f"pid={stale.pid}" in result.stdout
        assert "apps.webui.server" in result.stdout
        assert f"cwd={tmp_path}" in result.stdout
    finally:
        if stale.poll() is None:
            stale.kill()
            stale.wait(timeout=2)
