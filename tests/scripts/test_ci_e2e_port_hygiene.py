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

import contextlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HYGIENE = REPO_ROOT / "scripts" / "ci_e2e_port_hygiene.sh"
BACKEND_POOL_START = 8680
FRONTEND_POOL_START = 9400
POOL_SIZE = 120
PORT_LANE_STRIDE = 840
# The lane the previous version of this test bound unconditionally. A PR head
# that still carries that version keeps running it after this lands, so the
# rotation never offers lane 50 (Codex review on #1563).
LEGACY_LANE = 50


def _candidate_lanes() -> list[int]:
    """Lanes whose backend AND frontend windows sit above the host's ephemeral port range.

    The hygiene script kills every listener in the chosen lane's two 120-port windows.
    Below the ephemeral ceiling those windows can hold another runner's `bind(0)` test
    server, and killing that is a cross-runner casualty; above it, only what this test
    puts there can be there. Read from the kernel (60999 on agentbox, 48715 on nucbox),
    with the Linux default as the fallback when the file is absent (macOS).
    """
    ceiling = 60999
    with contextlib.suppress(OSError, ValueError, IndexError):
        ceiling = int(Path("/proc/sys/net/ipv4/ip_local_port_range").read_text().split()[1])
    lanes = [
        lane
        for lane in range(80)
        if BACKEND_POOL_START + lane * PORT_LANE_STRIDE > ceiling
        and FRONTEND_POOL_START + lane * PORT_LANE_STRIDE + POOL_SIZE <= 65535
        and lane != LEGACY_LANE
    ]
    assert len(lanes) >= 2, f"fewer than two usable lanes above ephemeral ceiling {ceiling}"
    return lanes


# The stale child claims a whole lane (every port of both windows, bind only)
# before it listens on the backend start port and announces the lane, so the
# choice, the reservation and the listener are one atomic step: nothing else
# on the host can hold a port inside the windows hygiene is about to sweep.
# A host above ephemeral ceiling 60999 offers four such lanes, and a lane is
# held only until its own hygiene pass kills the holder (about five seconds),
# so a child that finds every lane taken waits and retries for up to 60 s
# rather than failing the fifth concurrent run outright (Codex review, #1563).
STALE_LISTENER_SOURCE = """
import os, socket, sys, time
lanes = [int(x) for x in sys.argv[1].split(',')]
start = os.getpid() % len(lanes)
lanes = lanes[start:] + lanes[:start]
deadline = time.monotonic() + 60
claimed = None
while claimed is None and time.monotonic() < deadline:
    for lane in lanes:
        held = []
        ok = True
        for base in (8680, 9400):
            for port in range(base + lane * 840, base + lane * 840 + 120):
                s = socket.socket()
                try:
                    s.bind(('127.0.0.1', port))
                except OSError:
                    s.close()
                    ok = False
                    break
                held.append(s)
            if not ok:
                break
        if ok:
            claimed = (lane, held)
            break
        for s in held:
            s.close()
    if claimed is None:
        time.sleep(0.5)
if claimed is None:
    print('NO_FREE_LANE', flush=True)
    sys.exit(3)
lane, held = claimed
held[0].listen()
print(lane, flush=True)
time.sleep(60)
"""


@pytest.mark.skipif(shutil.which("lsof") is None, reason="lsof unavailable")
def test_hygiene_terminates_and_logs_a_real_stale_lane_listener(tmp_path: Path) -> None:
    """A real leaked engine listener is killed before the next pair claim."""
    candidates = _candidate_lanes()
    stale = subprocess.Popen(
        [
            sys.executable,
            "-c",
            STALE_LISTENER_SOURCE,
            ",".join(map(str, candidates)),
            "apps.webui.server",
        ],
        cwd=tmp_path,
        text=True,
        stdout=subprocess.PIPE,
    )
    try:
        assert stale.stdout is not None
        announced = stale.stdout.readline().strip()
        assert announced.isdigit(), f"stale listener found no free lane: {announced!r}"
        LANE = int(announced)
        assert LANE in candidates and LANE != LEGACY_LANE, LANE
        BACKEND_PORT = BACKEND_POOL_START + LANE * PORT_LANE_STRIDE
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
