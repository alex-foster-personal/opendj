"""perf-lab A/B: measure a fix against base in alternating untraced windows.

Brings up a base stack and a fix stack (stack.py, each on its own worktree,
dependencies and data copy, from the same fixture source), runs abwin.mjs
(fresh browser per side per rep, ABBA order across reps, one browser at a time)
and tears both stacks down, whatever happened. It then reports only the deltas
that exceed twice their combined standard error; everything else is within
noise, and a metric that lost a rep on either side is UNKNOWN.

The two stacks' manifests (manifest.py) must agree on everything but the code
under test (manifest.CODE_UNDER_TEST: commit, dirty diff, lockfiles); any other
difference refuses the A/B as UNKNOWN before a window runs. Each worktree is
re-measured against its manifest before the windows and after them, and a
change (manifest.py drift) makes the A/B UNKNOWN. A lockfile
difference is measured, not refused, and is reported as "deps differ".

Usage (Linux: abwin.mjs reads /proc; xvfb-run supplies the display):
  uv run --locked python scripts/perf/lab/ab.py --base-worktree ../music-dj-tools-wt-base \
      --fix-worktree ../music-dj-tools-wt-fix --data-src DATA --ids a,b,c,d --out OUT [--reps 3] [--win 10]
Output: OUT/ab.json (abwin's rows and summary), OUT/abwin.log, OUT/report.json
(with both manifests).
Exit 0 when the comparison is complete, 3 when it is UNKNOWN or PARTIAL,
2 when it could not start. Both stacks run clean trees (stack.py up refuses a
dirty one), and both are re-verified after the windows: a side whose HEAD or
tree changed, or whose server died or lost its port, during the run makes
the whole comparison UNKNOWN.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
# this file runs as a script: the harness root first, so its own scripts package wins
sys.path.insert(0, str(HERE.parents[2]))

from scripts.perf.lab import manifest as stack_manifest  # noqa: E402
from scripts.perf.lab.envs import harness_env  # noqa: E402

SIDES = (("A", "ab-base", "base_worktree"), ("B", "ab-fix", "fix_worktree"))


def _stack(*args: str) -> tuple[int, dict[str, Any] | None, str]:
    """stack.py's exit code, its last JSON line (up prints the state), and stderr.
    stack.py gets the operator's environment on purpose: it reads
    PERFLAB_SCRATCH, and builds every launch of its own from envs.py."""
    r = subprocess.run([sys.executable, str(HERE / "stack.py"), *args], capture_output=True, text=True, check=False)
    state = None
    lines = r.stdout.strip().splitlines()
    if r.returncode == 0 and lines:
        try:
            state = json.loads(lines[-1])
        except json.JSONDecodeError:
            state = None
    return r.returncode, state, r.stderr


def stack_drift(stacks: dict[str, dict[str, Any]]) -> str | None:
    """Why a stack's worktree or browser no longer matches the manifest `up`
    recorded (manifest.code_drift), or None. Vite serves the worktree live, so
    an edit, checkout or lockfile change after `up` would be measured under the
    old provenance, as would a replaced Chromium; one that cannot be measured
    is UNKNOWN too."""
    for side, s in stacks.items():
        try:
            keys = stack_manifest.code_drift(s)
        except (OSError, KeyError, subprocess.CalledProcessError) as e:
            return f"{side} stack: cannot measure drift: {type(e).__name__}: {e}"
        if keys:
            return f"{side} stack changed since up: {', '.join(keys)}"
    return None


def stack_ownership(names: dict[str, str]) -> str | None:
    """Why a side's stack is no longer served by the processes `up` launched
    (stack.py show: each recorded engine and vite pid has its create time and
    holds its port's listener), or None. A dead vite whose port another
    checkout took answers the same URL, so abwin would measure that page under
    this manifest; checked before the first window and after the last."""
    for side, name in names.items():
        rc, _state, err = _stack("show", "--name", name)
        if rc != 0:
            return f"{side} stack {name} is not the stack it launched (stack.py show exit {rc}): {err.strip()[-600:]}"
    return None


def media_fault(rc: int, ab: dict[str, Any]) -> str | None:
    """Why the inputs each side served, hashed around its windows (abwin.mjs
    checkMedia), are not known to be the bytes measured, or None."""
    if "media_faults" not in ab:
        return f"abwin exit {rc}, ab.json records no input digests, so the measured bytes are unknown"
    if ab["media_faults"]:
        n = len(ab["media_faults"])
        return f"abwin exit {rc}, {n} measured inputs changed or could not be hashed: {ab['media_faults'][0][:200]}"
    return None


def launch_identity_fault(rc: int, ab: dict[str, Any]) -> str | None:
    """Why the browser launches of one abwin run did not all share one
    graphics stack and audio output (abwin.mjs stampLaunch), or None. A side
    or rep that fell back to SwiftShader, or played through another output,
    measured other work, as does one whose identity was never read."""
    if "identity_faults" not in ab or not ab.get("identity"):
        return f"abwin exit {rc}, ab.json records no browser launch identity (graphics, audio output)"
    if ab["identity_faults"]:
        n = len(ab["identity_faults"])
        return (
            f"abwin exit {rc}, {n} browser launches ran on another graphics stack or audio output, "
            f"or could not be read: {ab['identity_faults'][0][:200]}"
        )
    return None


def classify(rc: int, ab: dict[str, Any] | None, drift: list[str] | None = None) -> tuple[str, str | None]:
    """OK, PARTIAL or UNKNOWN for one abwin run, and why when not OK. abwin
    rewrites ab.json after every rep, so a run killed midway leaves rows and no
    summary; a run whose metric lost a rep on a side has no complete answer;
    and a side whose stack stopped verifying during the run (drift, from
    tree_drift: its tree changed, or a server died or lost its port) ran code
    its head does not name, so no comparison of it is an answer."""
    if drift:
        return (
            "UNKNOWN",
            f"a side's stack stopped verifying during the run, so its head is not what ran: {'; '.join(drift)}",
        )
    if not ab or "summary" not in ab:
        return "UNKNOWN", f"abwin exit {rc}, ab.json has no final summary (run did not finish)"
    if why := media_fault(rc, ab) or launch_identity_fault(rc, ab):
        return "UNKNOWN", why
    if ab.get("incomplete"):
        n = len(ab["incomplete"])
        return "UNKNOWN", f"abwin exit {rc}, {n} metrics lost a rep on a side: {', '.join(ab['incomplete'][:5])}"
    if ab.get("errors"):
        return "PARTIAL", f"abwin exit {rc}, {len(ab['errors'])} errors recorded, first: {ab['errors'][0][:200]}"
    if rc != 0:
        return "UNKNOWN", f"abwin exit {rc} with no recorded error"
    return "OK", None


def instrument(
    ab: dict[str, Any] | None, before: tuple[str, dict[str, Any]], after: tuple[str, dict[str, Any]]
) -> tuple[dict[str, Any] | None, str | None]:
    """(the A/B's measurement identity, None), or (None, why it is unknown).
    abwin.mjs, its statistics helpers, node and Playwright come from this
    harness checkout, not from either stack, so the report names them: the lab
    digest ab.py took before the stacks came up (manifest.harness_digest), the
    one abwin stamped, and the one after the windows must all agree, and abwin
    must have recorded the node and Playwright it ran on. before and after
    are ab.py's own manifest.harness_stamp, taken before the stacks come up
    and after the windows; abwin verifies its stamp the same way before its
    final write (harness_verified) and records harness_fault when it moved."""
    if before[0] != after[0]:
        return None, f"the measurement harness changed during the A/B ({before[0][:12]} -> {after[0][:12]})"
    if moved := stack_manifest.harness_moved(before, after):
        return None, f"ab.py: {moved}"
    if ab and ab.get("harness_fault"):
        return None, f"abwin: {ab['harness_fault']}"
    if not ab or not ab.get("harness") or not ab.get("measurement_deps"):
        return None, "ab.json records no harness digest or measurement_deps: the measuring code is unknown"
    if ab.get("harness_verified") is not True:
        return (
            None,
            "abwin never verified its harness after measuring (no final ab.json): the measuring code is unknown",
        )
    if ab["harness"] != before[0]:
        return None, f"abwin ran other harness code than ab.py hashed ({ab['harness'][:12]} vs {before[0][:12]})"
    return {"code": before[0], "deps": {"abwin": ab["measurement_deps"], "ab": before[1]}}, None


def resolved_deltas(summary: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """Every B - A delta beyond twice its combined standard error, and how many
    complete comparisons stayed within that noise floor."""
    out: list[dict[str, Any]] = []
    within = 0
    for state, metrics in summary.items():
        for metric, c in metrics.items():
            if c.get("resolved") is True:
                out.append({**c, "state": state, "metric": metric, "A": c["A"]["mean"], "B": c["B"]["mean"]})
            elif c.get("resolved") is False:
                within += 1
    return out, within


def tree_drift(stack: Any = None) -> list[str]:
    """Why each side's head no longer names the code that ran ([] when both
    still do): stack.py verify on both stacks (tree and serving processes),
    after the windows and before they are torn down."""
    stack = stack or _stack
    drift = []
    for side, name, _attr in SIDES:
        rc, _state, err = stack("verify", "--name", name)
        if rc != 0:
            drift.append(f"{side}: {err.strip()[-300:] or f'verify exit {rc}'}")
    return drift


# Every file ab.py or abwin.mjs writes into --out; nothing else there is touched.
OWNED = ("ab.json", "abwin.log", "report.json")


def _clear_owned(out: Path) -> None:
    """Create out and delete every file of OWNED in it, before setup: an early
    exit (a stack that will not come up) then never leaves a prior run's result
    there. Nothing else in out is touched."""
    out.mkdir(parents=True, exist_ok=True)
    for owned in OWNED:
        (out / owned).unlink(missing_ok=True)


def _runner(which: Any = shutil.which) -> tuple[list[str] | None, str | None]:
    """The argv prefix that runs abwin.mjs: the repo's version-checked node
    under xvfb-run, or node alone on a display. Resolved before any stack starts.
    The floor is THIS checkout's on purpose: abwin.mjs (and driver.mjs) are
    harness code, run from this checkout against the stacks, never code of the
    worktree under test, unlike vite (stack._node, held to the target's floor)."""
    from scripts.perf.node_runtime import resolved_node

    try:
        node = resolved_node()
    except SystemExit as e:
        return None, str(e)
    xvfb = which("xvfb-run")
    if xvfb:
        # launched by its real path, never a PATH lookup a relative entry could answer
        if not Path(xvfb).is_absolute():
            return None, f"xvfb-run is not on PATH as an absolute executable (found {xvfb!r})"
        return [str(Path(xvfb).resolve()), "-a", node], None
    if os.environ.get("DISPLAY"):
        return [node], None
    return None, "no xvfb-run on PATH and no DISPLAY: abwin.mjs launches a headed Chromium"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-worktree", required=True)
    ap.add_argument("--fix-worktree", required=True)
    ap.add_argument("--data-src", required=True)
    ap.add_argument("--ids", required=True)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--win", type=int, default=10)
    ap.add_argument("--states", default="idle-empty,loaded-stopped,four-playing")
    ap.add_argument("--chrome")
    a = ap.parse_args()
    if not sys.platform.startswith("linux"):
        print("[FAIL] abwin.mjs reads CPU ticks from Linux /proc; run the A/B on Linux", file=sys.stderr)
        return 2
    runner, why = _runner()
    if runner is None:
        print(f"[FAIL] {why}", file=sys.stderr)
        return 2
    _clear_owned(a.out)
    harness_before = stack_manifest.harness_stamp(())
    stacks: dict[str, dict[str, Any]] = {}
    chrome = ["--chrome", a.chrome] if a.chrome else []
    drift: list[str] = []
    try:
        for side, name, attr in SIDES:
            up = ("up", "--name", name, "--worktree", getattr(a, attr), "--data-src", a.data_src, *chrome)
            rc, state, err = _stack(*up)
            if state is None:
                print(f"[FAIL] {side} stack did not come up (exit {rc}): {err.strip()[-600:]}", file=sys.stderr)
                return 2
            stacks[side] = state
        manifests = {side: s["manifest"] for side, s in stacks.items()}
        refused, deps = stack_manifest.verdict(manifests["A"], manifests["B"])
        names = {side: name for side, name, _attr in SIDES}
        refused = refused or stack_drift(stacks) or stack_ownership(names)
        if refused:
            report = {"status": "UNKNOWN", "reason": refused, "manifests": manifests}
            (a.out / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8", newline="\n")
            print(f"[UNKNOWN] {refused}; the A/B measures only the code under test, so nothing ran")
            return 3
        cmd = [*runner, str(HERE / "abwin.mjs"), "--a", stacks["A"]["url"], "--b", stacks["B"]["url"]]
        cmd += ["--a-engine-pid", str(stacks["A"]["engine_pid"]), "--b-engine-pid", str(stacks["B"]["engine_pid"])]
        cmd += ["--ids", a.ids, "--out", str(a.out), "--reps", str(a.reps), "--win", str(a.win), "--states", a.states]
        # the Chromium both manifests name (equal browser_sha256), never a fresh resolution
        cmd += ["--chrome", stacks["A"]["browser"]]
        with open(a.out / "abwin.log", "w", encoding="utf-8", newline="\n") as log:
            # abwin's node takes envs.harness_env: no NODE_OPTIONS or other runtime setting
            rc = subprocess.run(
                cmd, stdout=log, stderr=subprocess.STDOUT, env=harness_env(os.environ), check=False
            ).returncode
        # an edit during the windows was measured too, and so was a server that took a dead stack's port
        drift = tree_drift()
        drifted = stack_drift(stacks) or stack_ownership(names)
    finally:
        for side, name, _attr in SIDES:
            if side in stacks:
                down_rc, _state, err = _stack("down", "--name", name)
                if down_rc != 0:
                    print(f"[WARN] {side} stack teardown: {err.strip()}", file=sys.stderr)
    f = a.out / "ab.json"
    ab = json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
    harness, unmeasured = instrument(ab, harness_before, stack_manifest.harness_stamp(()))
    # every fault is UNKNOWN; a side's tree drift (stack.py verify) is named first
    if (drifted or unmeasured) and not drift:
        status, reason = "UNKNOWN", drifted or unmeasured
    else:
        status, reason = classify(rc, ab, drift)
    deltas, within = resolved_deltas(ab.get("summary", {})) if ab else ([], 0)
    heads = {side: m["commit"] for side, m in manifests.items()}
    report = {
        "status": status,
        "reason": reason,
        "heads": heads,
        "deps_differ": deps,
        "manifests": manifests,
        # the code and libraries that measured it (instrument)
        "harness": harness,
        "resolved": deltas,
        "within_noise": within,
    }
    (a.out / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8", newline="\n")
    print(f"[{status}] A={heads['A'][:9]} B={heads['B'][:9]}{f': {reason}' if reason else ''}")
    if deps:
        print(f"  deps differ: {', '.join(deps)} (measured as part of the change)")
    for d in deltas:
        print(f"  {d['state']:15} {d['metric']:24} A {d['A']} B {d['B']} delta {d['delta']} (> 2SE {d['noise_2se']})")
    print(f"  {within} other complete comparisons within noise (|delta| <= 2SE)")
    return 0 if status == "OK" else 3


if __name__ == "__main__":
    sys.exit(main())
