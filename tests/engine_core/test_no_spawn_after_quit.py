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
  - if the coverage drain reports a refused stems probe as a tick failure (an
    ERROR, so a Sentry event) instead of ending its loop -> broken (OPEN-DJ-BE-A7)
  - if the coverage drain spends a track's attempt on a refused job -> broken
  - if the coverage drain starts a tick once the quit has begun -> broken
  - if the guard refuses spawns BEFORE the quit then nothing runs at all (the
    overshoot control, in every case) -> broken
"""
from __future__ import annotations

import json
import os
import shutil
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


def _run(body: str, mode: str, tmp_path: Path, env: dict[str, str] | None = None) -> dict:
    out = subprocess.run(
        [sys.executable, "-c", _PROLOGUE + body, mode, str(tmp_path)],
        cwd=REPO, capture_output=True, text=True, timeout=120, check=False, env=env,
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


#: A real coverage drain over a real state.db and the real coverage snapshot.
#: ``MDT_DATA_DIR`` points every path the snapshot reads at the case's tmp dir.
#: The SIGTERM lands while the tick reads its snapshot ("quitting"), the race
#: the Sentry event came from, or before the loop's first wake ("quit_early").
_COVERAGE = """
import logging, threading, time
from pathlib import Path
from fastapi import FastAPI
from apps.webui.server import coverage_drain as cd
from apps.webui.server import coverage_outcomes as co
from apps.webui.server import coverage_stems_terminal as st
from apps.webui.server.routes import ingest as ingest_mod
from tests.health_lights import fixtures as fx

MODE, tmp = sys.argv[1], Path(sys.argv[2])
data = tmp / "data"
state_db = fx.make_state_db(data)
stems = data / "state" / "stems"
app = FastAPI()
app.state.state_db = state_db
app.state.stem_roots = (stems,)
app.state.stems_source_refusal_fn = lambda: None
outcomes = co.OutcomeStore(co.store_path(data))

records = []
class Keep(logging.Handler):
    def emit(self, record):
        records.append([record.levelname, record.getMessage()])
drain_log = logging.getLogger(cd.__name__)
drain_log.addHandler(Keep())
drain_log.setLevel(logging.DEBUG)

snapshots = []
def snapshot():
    snapshots.append(MODE)
    measured = ingest_mod.build_snapshot(app)
    if MODE == "quitting":
        arm_quit_watchdog(60.0)
    return measured

def ledger():
    return {f"{step}:{sid}": [o.kind, o.attempts, o.reason] for (step, sid), o in outcomes.load().items()}

def build(jobs, stems_check=None):
    return cd.CoverageDrain(
        snapshot_fn=snapshot, jobs=jobs, playing_fn=lambda: False, outcomes=outcomes,
        config=cd.DrainConfig(cd.config_path(data)), stems_check=stems_check,
    )
"""

_STEMS_LOOP = _COVERAGE + """
# A track with no stem bundle: the tick's stems check probes it with ffprobe.
fx.seed_track(state_db, "a", str(fx.audio_file(tmp / "music", "a.mp3")))
check = st.StemsCheck(
    outcomes=outcomes, keep=st.KeepPending(st.keep_pending_path(data)), title_fn=lambda _sid: None,
)
drain = build({}, check)
if MODE == "quit_early":
    arm_quit_watchdog(60.0)
drain.start()
thread = drain._thread
deadline = time.monotonic() + 15
while time.monotonic() < deadline:
    if not thread.is_alive() or drain.status().ticks or any(lv == "ERROR" for lv, _ in records):
        break
    time.sleep(0.05)
time.sleep(0.2)  # let the loop settle after the event it reached
status = drain.status()
result = {
    "exited_by_itself": not thread.is_alive(),
    "ticks": status.ticks, "state": status.state, "reason": status.reason,
    "snapshots": len(snapshots), "errors": [m for lv, m in records if lv == "ERROR"],
    "info": [m for lv, m in records if lv == "INFO"],
}
drain.stop()
print(json.dumps(result))
"""

_VOCALS_JOB = _COVERAGE + """
# A track with stems and no vocals: the tick runs one vocals job. The job is
# the drain's injected job seam (the thing under test is the drain); it starts
# a real child that fails, the way a broken vocals worker would.
fx.seed_track(state_db, "v", str(fx.audio_file(tmp / "music", "v.mp3")))
fx.write_stem_bundle(stems, "v")
started = []
def vocals(stable_id, audio_path):
    child = subprocess.run([sys.executable, "-c", "raise SystemExit(1)"], check=False)
    started.append(stable_id)
    if child.returncode != 0:
        raise RuntimeError(f"vocals from-stems exited {child.returncode}")
drain = build({"vocals": vocals})
try:
    outcome = drain.tick()
except Exception as exc:
    outcome = type(exc).__name__
status = drain.status()
print(json.dumps({
    "outcome": outcome, "child_started": started, "ledger": ledger(),
    "jobs_failed": status.jobs_failed, "last_job": status.last_job,
    "warnings": [m for lv, m in records if lv in ("WARNING", "ERROR")],
}))
"""

needs_probe = pytest.mark.skipif(
    shutil.which("ffprobe") is None and shutil.which("ffmpeg") is None,
    reason="UNAVAILABLE: neither ffprobe nor ffmpeg is installed, so the stems check starts no process",
)


def _data_env(tmp_path: Path) -> dict[str, str]:
    return {**os.environ, "MDT_DATA_DIR": str(tmp_path / "data")}


@needs_probe
@pytest.mark.parametrize("mode", ["running", "quitting", "quit_early"])
def test_the_coverage_drain_loop_ends_cleanly_once_quitting(mode: str, tmp_path: Path) -> None:
    """[if] the quit refuses the coverage drain's stems probe [then] its loop ends with no error, [else stop]."""
    got = _run(_STEMS_LOOP, mode, tmp_path, _data_env(tmp_path))
    if mode == "running":
        # The control: the same drain ticks, probes for real, and keeps running.
        assert got["ticks"] >= 1 and not got["exited_by_itself"] and got["errors"] == [], got
        assert got["state"] == "ran:stems-check", got
        return
    assert got["errors"] == [], (
        f"if the quit's refusal is logged as a tick failure (a Sentry event) then broken: {got}"
    )
    assert got["exited_by_itself"], f"if the drain keeps ticking once quitting then broken: {got}"
    assert got["state"] == "stopped" and got["reason"] == "the engine is quitting", got
    assert any("quitting" in line for line in got["info"]), got
    if mode == "quit_early":
        assert got["snapshots"] == 0, (
            f"if the drain takes a snapshot after the quit began then broken: {got}"
        )


@pytest.mark.parametrize("mode", ["running", "quitting"])
def test_the_coverage_drain_spends_no_attempt_on_a_refused_job(mode: str, tmp_path: Path) -> None:
    """[if] the quit refuses a coverage drain job's process [then] no track attempt is spent, [else stop]."""
    got = _run(_VOCALS_JOB, mode, tmp_path, _data_env(tmp_path))
    if mode == "running":
        # The control: a real job failure is still recorded and spends an attempt.
        assert got["outcome"] == "failed:vocals" and got["child_started"] == ["v"], got
        kind, attempts, reason = got["ledger"]["vocals:v"]
        assert (kind, attempts) == ("failed", 1) and "exited 1" in reason, got
        assert got["jobs_failed"] == 1, got
        return
    assert got["child_started"] == [], got
    assert got["ledger"] == {}, f"if the quit's refusal spends a track attempt then broken: {got}"
    assert got["jobs_failed"] == 0 and got["last_job"] is None, got
    assert got["warnings"] == [], got
    assert got["outcome"] == "SpawnAfterQuit", (
        f"if the refusal does not reach the loop's clean exit then broken: {got}"
    )
