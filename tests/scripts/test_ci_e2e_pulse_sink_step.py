"""The e2e audio-sink lifecycle runs through one script, under one host lock.

The PulseAudio server is shared by every job of one user on a host, so the
setup (probe, load a runner-named null sink, make it default) and the restore
(put a durable default back only while it is still ours, unload only our
module) are each one critical section under the `pulse-sink` host lock. The
behaviour itself lives in `scripts/ci_pulse_sink.sh`; this test pins that the
workflow uses it, locked, in both jobs, and restores even when a suite is red.

Regression lines:
  - if a job sets up or restores the sink without the pulse-sink lock then two
    jobs race the shared server (TOCTOU between ownership check and mutation)
  - if a job has no always-run restore after its last suite then a shared
    server carries this job's sink into the next job
"""

from __future__ import annotations

from pathlib import Path

import yaml

E2E = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "e2e.yml"
LOCKED_SETUP = "scripts/ci_host_lock.sh pulse-sink scripts/ci_pulse_sink.sh setup"
LOCKED_RESTORE = "scripts/ci_host_lock.sh pulse-sink scripts/ci_pulse_sink.sh restore"


def test_both_jobs_run_the_sink_lifecycle_locked_and_always_restore() -> None:
    """[if] a job sets up or restores the sink unlocked, or skips restore on red [then] fail,
    [else stop]."""
    doc = yaml.safe_load(E2E.read_text(encoding="utf-8"))
    seen = 0
    for job, spec in doc["jobs"].items():
        steps = spec["steps"]
        names = [st.get("name") or "" for st in steps]
        runs = [st.get("run") or "" for st in steps]
        sink = next((i for i, n in enumerate(names) if "Virtual audio sink" in n), None)
        if sink is None:
            continue
        seen += 1
        assert LOCKED_SETUP in runs[sink], f"{job}: sink setup is not the locked script"
        assert "pulseaudio --start" not in runs[sink] and "load-module" not in runs[sink], (
            f"{job}: sink setup must not be inlined beside the script"
        )
        restore = [i for i, n in enumerate(names) if "Restore the PulseAudio server" in n]
        assert len(restore) == 1, f"{job}: expected one restore step, found {len(restore)}"
        r = steps[restore[0]]
        assert str(r.get("if", "")).strip() == "always()", f"{job}: restore must always run"
        assert LOCKED_RESTORE in r["run"], f"{job}: restore is not the locked script"
        last_suite = max(i for i, run in enumerate(runs) if "playwright test" in run)
        assert restore[0] > last_suite, f"{job}: restore must follow the last suite"
    assert seen == 2, f"expected the gate and extended jobs, saw {seen}"
