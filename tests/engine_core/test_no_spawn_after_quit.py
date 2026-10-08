"""No process starts once the engine has begun to quit (W43, INSTALL-36).

[if] the engine tries to start a process after the quit's signal [then] the start is refused, [else stop].

A fork that lands after the watchdog's last look would outlive the engine.

The watchdog reaps descendants for a bounded window and then exits, so no look
can catch a fork that completes after the last one: the job runner alone
tolerates five seconds between a claim and its fork, and the coverage drain can
launch after a snapshot that outlived the window. One audit hook refuses every
process start once the watchdog is armed. Every case here runs in a fresh
process with the real guard, the real ``arm_quit_watchdog`` that the SIGTERM
handler calls, and real process creation; nothing is patched.

Regression lines:
  - if a subprocess, asyncio or os spawn starts after the quit began -> broken
  - if the job runner claims a queued job after the quit began then the job
    fails at its fork instead of waiting for the next boot -> broken
  - if a job claimed before the quit forks after it then its worker leads its
    own session past every kill -> broken
  - if the ahead-analysis drain counts a refused run against its tracks -> broken
  - if the guard refuses spawns BEFORE the quit then nothing runs at all (the
    overshoot control, in every case) -> broken
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.requirement("INSTALL-36")

REPO = Path(__file__).resolve().parents[2]

#: The prologue every case shares: the engine's guard, then a quit armed for
#: real (the watchdog's deadline is far past the case, which exits first).
_PROLOGUE = """
import asyncio, json, os, subprocess, sys
from apps.shared.shutdown_bound import arm_quit_watchdog, install_spawn_guard
install_spawn_guard()
QUIT = sys.argv[1] == "quitting"
def arm():
    if QUIT:
        arm_quit_watchdog(60.0)
"""


def _run(body: str, mode: str, tmp_path: Path) -> dict:
    out = subprocess.run(
        [sys.executable, "-c", _PROLOGUE + body, mode, str(tmp_path)],
        cwd=REPO, capture_output=True, text=True, timeout=120, check=False,
    )
    assert out.returncode == 0, out.stderr[-3000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


_SPAWNS = """
def attempt(start):
    try:
        start()
    except Exception as exc:
        return type(exc).__name__
    return "started"

async def via_asyncio():
    proc = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
    await proc.wait()

arm()
print(json.dumps({
    "subprocess": attempt(lambda: subprocess.run([sys.executable, "-c", "pass"], check=True)),
    "asyncio": attempt(lambda: asyncio.run(via_asyncio())),
    "posix_spawn": attempt(lambda: os.waitpid(
        os.posix_spawn(sys.executable, [sys.executable, "-c", "pass"], os.environ), 0)),
}))
"""


@pytest.mark.skipif(sys.platform == "win32", reason="os.posix_spawn is POSIX-only")
@pytest.mark.parametrize("mode", ["running", "quitting"])
def test_no_process_starts_once_the_quit_has_begun(mode: str, tmp_path: Path) -> None:
    got = _run(_SPAWNS, mode, tmp_path)
    want = "SpawnAfterQuit" if mode == "quitting" else "started"
    assert got == dict.fromkeys(("subprocess", "asyncio", "posix_spawn"), want), got


_JOBS = """
from pathlib import Path
from apps.engine_core.jobs.runner import JobRunner, register_worker
from apps.engine_core.jobs.store import JobStore

tmp = Path(sys.argv[2])
register_worker("mark", lambda p: [sys.executable, "-c",
    "import pathlib, sys; pathlib.Path(sys.argv[1]).touch()", p["mark"]])
store = JobStore(tmp / "jobs.db", boot_id="boot-a", owner_pid=os.getpid())
store.recover()

async def drive():
    runner = JobRunner(store, poll_s=0.01, max_concurrent=1)
    # A job claimed BEFORE the quit, whose task reaches its fork after it.
    early = store.enqueue("mark", {"mark": str(tmp / "early")})["id"]
    for job in store.claim_queued(limit=1):
        runner._spawn(job)
    arm()
    late = store.enqueue("mark", {"mark": str(tmp / "late")})["id"]
    await runner.start()
    await asyncio.sleep(1.0)  # many supervisor polls
    await asyncio.gather(*runner._tasks.values(), return_exceptions=True)
    await runner.stop()
    return store.get(early), store.get(late)

early, late = asyncio.run(drive())
print(json.dumps({
    "early": [early["status"], early["error"] or "", (tmp / "early").exists()],
    "late": [late["status"], (tmp / "late").exists()],
}))
"""


@pytest.mark.parametrize("mode", ["running", "quitting"])
def test_the_job_runner_starts_no_worker_once_quitting(mode: str, tmp_path: Path) -> None:
    got = _run(_JOBS, mode, tmp_path)
    if mode == "quitting":
        status, error, ran = got["early"]
        assert not ran and status == "failed" and "engine is quitting" in error, (
            f"if a job claimed before the quit forks after it then broken: {got}"
        )
        assert got["late"] == ["queued", False], (
            f"if a quitting engine claims a queued job then broken: {got}"
        )
    else:
        assert got == {"early": ["succeeded", "", True], "late": ["succeeded", True]}, got


_DRAIN = """
from apps.webui.server import ahead_analysis
arm()
run = ahead_analysis.run_lane_via_queue(str(os.path.join(sys.argv[2], "none.db")), lambda: None)
print(json.dumps(run("loudness", "own", ["t0", "t1"])))
"""


@pytest.mark.parametrize("mode", ["running", "quitting"])
def test_the_ahead_drain_fails_no_track_for_a_refused_run(mode: str, tmp_path: Path) -> None:
    got = _run(_DRAIN, mode, tmp_path)
    if mode == "quitting":
        assert got == {}, f"if the quit's refusal is counted against the tracks then broken: {got}"
    else:
        # The control: a real queue CLI ran against a missing db and failed.
        assert set(got) == {"t0", "t1"} and all("failed" in v for v in got.values()), got
