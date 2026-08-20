#!/usr/bin/env python3
"""Savepoint gate: one command that proves the trunk deserves a savepoint tag.

Requirements (docstring mini-PRD):
- ✔︎ ✅ Runs every fast suite in a fixed order and stops at the first failure.
    [if] any step exits non-zero [then ⛔️] the gate exits non-zero naming the step
    [if] all steps pass [then] a per-step timing table and PASS line print
- ✔︎ ✅ Frontend steps run svelte-kit sync FIRST (unsynced worktrees cascade into
    ~46 phantom failures and a 10 minute hang).
    [if] sync is skipped on a fresh worktree [then ⛔️] node --test would hang; the
    gate never offers an order without sync
- ✔︎ ✅ --fast skips the two slow steps (full pytest, e2e smoke) for inner-loop use.
    [if] --fast passes [then] it proves units + types only, and the summary SAYS so
- ✔︎ ✅ The pytest step runs in parallel and still cannot shrink unnoticed.
    [if] fewer than PYTEST_COLLECT_FLOOR tests collect [then ⛔️] pytest exits
    non-zero naming the count, and the gate fails at pytest-full

Steps and measured baselines (Mon 17 Aug 2026, trunk 7d1f4ca2):
  1. svelte-kit sync          ~5s
  2. frontend unit suite      612 tests   ~21s
  3. svelte-check             0 errors    ~7s
  4. full pytest              3600+ tests ~3m25s   (skipped by --fast)
  5. savepoint e2e smoke      6 tests     ~20-30s  (skipped by --fast)

Total ~4-5 minutes full, ~35s with --fast.

Round 2 (Thu 20 Aug 2026, this Mac, 10 logical cores) put pytest-xdist under
step 4. Measured on a pinned tree with identical fixtures, full suite, warm,
serial and parallel runs interleaved so they shared load conditions: single
process median 117.3s -> `-n auto --dist loadgroup` median 43.4s, a 2.7x
speedup worth ~74s per gate run. Nothing about WHAT runs changed: 3782 pass
and 80 skip either way, and --collect-floor is what PROVES that rather than
asserting it in a comment.
"""

from __future__ import annotations

import argparse
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = REPO_ROOT / "apps" / "webui" / "frontend"

# xdist workers for the full pytest step. `auto` = one per logical core.
# Measured Thu 20 Aug 2026 on a 10-core Mac (6 performance + 4 efficiency),
# full suite, warm, same fixtures, wall time from pytest's own summary:
#   single process   130.00 / 117.27 / 112.69   median 117.3s
#   -n 6              67.38 /  45.40 /  45.72   median  45.7s
#   -n auto (10)      43.39 /  41.55 /  47.90 /  43.38   median  43.4s
# `auto` won, so the machine-specific number was dropped rather than kept and
# justified after the fact. CI is the exception and runs SERIAL: `-n auto` was
# measured on the ubuntu runner and came out SLOWER (295s against 273-289s
# serial), because 4 vCPUs plus per-worker cold imports plus pytest-cov's
# combine step eat the gain. See ops/test-latency/README.md.
PYTEST_WORKERS = "auto"

# `--dist loadgroup` rather than the default `load`, so a suite that genuinely
# must share a worker can say so with @pytest.mark.xdist_group instead of the
# whole run being serialised for it. Nothing carries the marker today; the mode
# is set now so the escape hatch exists when something needs it.
PYTEST_DIST = "loadgroup"

# Minimum tests the full suite must COLLECT. Measured Thu 20 Aug 2026: 3861
# locally with the analysis + tags extras present. The floor sits below that
# with room for the three modules that skip at import time off macOS, and it
# only ever ratchets UP. Its job is to make `-n` safe: parallel execution
# changes distribution, not membership, so a run that collects less has lost
# tests and must fail rather than report a smaller green.
PYTEST_COLLECT_FLOOR = "3700"


@dataclass
class Step:
    name: str
    argv: list[str]
    cwd: Path
    slow: bool = False


def _steps() -> list[Step]:
    return [
        Step(
            "svelte-kit-sync",
            ["pnpm", "exec", "svelte-kit", "sync"],
            FRONTEND_DIR,
        ),
        Step(
            "frontend-unit",
            ["node", "--test", "--test-concurrency=1"]
            + sorted(str(p) for p in (FRONTEND_DIR / "tests" / "unit").glob("*.test.mjs")),
            FRONTEND_DIR,
        ),
        Step(
            "svelte-check",
            ["pnpm", "check"],
            FRONTEND_DIR,
        ),
        Step(
            "pytest-full",
            [
                "uv", "run", "--with", "modal", "pytest", "-q",
                "-n", PYTEST_WORKERS,
                "--dist", PYTEST_DIST,
                "--collect-floor", PYTEST_COLLECT_FLOOR,
            ],
            REPO_ROOT,
            slow=True,
        ),
        Step(
            "e2e-smoke",
            ["just", "savepoint-smoke"],
            REPO_ROOT,
            slow=True,
        ),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts/savepoint_gate.py")
    parser.add_argument(
        "--fast",
        action="store_true",
        default=False,
        help="skip the slow steps (full pytest, e2e smoke); proves units + types only",
    )
    args = parser.parse_args(argv)

    steps = [s for s in _steps() if not (args.fast and s.slow)]
    timings: list[tuple[str, float]] = []
    total_start = time.monotonic()

    for step in steps:
        print(f"[gate] {step.name} ...", flush=True)
        start = time.monotonic()
        result = subprocess.run(step.argv, cwd=step.cwd)
        elapsed = time.monotonic() - start
        timings.append((step.name, elapsed))
        if result.returncode != 0:
            print(f"[gate] FAIL at {step.name} after {elapsed:.1f}s", flush=True)
            return 1

    total = time.monotonic() - total_start
    print()
    print("| step | time |")
    print("|:---|---:|")
    for name, elapsed in timings:
        print(f"| {name} | {elapsed:.1f}s |")
    print(f"| **total** | **{total:.1f}s** |")
    scope = "FAST: units + types only" if args.fast else "FULL"
    print(f"[gate] PASS ({scope})", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
