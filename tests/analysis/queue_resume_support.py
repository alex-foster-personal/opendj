"""Shared helpers for queue resume tests (split from test_queue_resume)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from apps.analysis import admission
from apps.analysis.worker_sizing import HostCapacity

from .queue_probe_backends import (
    PROBE_DB_ENV,
    PROBE_DIR_ENV,
    PROBE_SLEEP_ENV,
    BeatgridProbeV1,
)

REPO_ROOT: Path = Path(__file__).resolve().parents[2]

#: Room enough that duration-band worker counts are not host-capped (NATIVE-32).
AMPLE_HOST: HostCapacity = HostCapacity(available_mb=16_000.0, cpu_count=8)


def _candidates(n: int, tmp_path: Path, *, minutes: float = 3.0) -> list:
    out = []
    for i in range(n):
        audio = tmp_path / f"t{i}.wav"
        audio.write_bytes(b"\0")
        out.append(
            admission.Candidate(
                stable_id=f"sid_{i}",
                lane="beatgrid",
                backend=BeatgridProbeV1.name,
                file_path=str(audio),
                duration_s=minutes * 60.0,
            )
        )
    return out


def _marker_counts(probe_dir: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in probe_dir.glob("*.json"):
        sid = json.loads(path.read_text())["stable_id"]
        counts[sid] = counts.get(sid, 0) + 1
    return counts


@pytest.fixture()
def probe_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "markers"
    d.mkdir()
    monkeypatch.setenv(PROBE_DIR_ENV, str(d))
    return d


# --- arm (b): a real process kill -----------------------------------------


def _spawn_runner(db: Path, batch_id: str, probe_dir: Path, sleep_s: float):
    env = {
        **os.environ,
        PROBE_DIR_ENV: str(probe_dir),
        PROBE_DB_ENV: str(db),
        PROBE_SLEEP_ENV: str(sleep_s),
        "PYTHONPATH": str(REPO_ROOT),
    }
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "apps.analysis.queue_cli",
            "--db",
            str(db),
            "--json",
            "run",
            "--batch-id",
            batch_id,
            "--backend",
            "tests.analysis.queue_probe_backends:BeatgridProbeV1",
        ],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        # Its own process group, so the kill below can take the runner AND
        # the ProcessPoolExecutor children it spawned. Killing the runner
        # pid alone reparents those children to init and leaves them
        # resident: measured on this Mac, one run of this test left a
        # `multiprocessing.spawn` worker and its `resource_tracker` alive
        # after the test passed. A suite that leaks two processes per run is
        # a suite that eventually takes the host down, and the docstring
        # below already claimed a process-group kill it was not performing.
        start_new_session=True,
    )


def _kill_group(proc: subprocess.Popen[str]) -> None:
    """SIGKILL the runner's whole process group, then prove it is gone.

    The assertion is the point: ``killpg`` returning cleanly says the signal
    was delivered, not that the group is empty, and an orphaned pool worker
    is exactly the thing that survives a signal aimed at one pid.
    """
    # Read the group id BEFORE the kill. After ``wait`` reaps the runner,
    # ``getpgid(proc.pid)`` raises whether or not the group still holds a
    # leaked worker, so a probe built on it could never report one.
    pgid = os.getpgid(proc.pid)
    os.killpg(pgid, signal.SIGKILL)
    proc.wait(timeout=30)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    raise AssertionError(f"process group {pgid} still has members after SIGKILL")
