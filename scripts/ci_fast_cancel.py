"""Cancel the rest of a CI run when the fast tier found a failure that is NEW against main.

SMARTEST-CI round 6 (specs/ci-fail-fast.md, part 4). A fast-tier leg that goes red on a
test main is already red on says nothing about the pull request; one that goes red on a
GENUINE identity says everything, and the slow shards would only repeat it fifteen minutes
later while holding runners. This step reads the leg's own pytest output, subtracts main's
red set, and cancels the run on a GENUINE residual.

The baseline is the watcher's (`scripts.ci_main_red`, PR #3293), never a fork of it: this
module imports `cached_main_red` when it is importable, or reads the same JSON shape from
``--main-red-json``. With neither, the baseline is UNKNOWN and nothing is cancelled: a
missing baseline must not turn every trunk flake into a cancelled run.

Exit codes: 0 = decision made (cancelled or not), 3 = UNKNOWN (no baseline; nothing done).
The leg's verdict is pytest's own exit code, decided before this step runs.

`gh run cancel` cancels the whole run, INCLUDING the job this step runs in (there is no
per-job cancel in the Actions API), so the job's conclusion becomes "cancelled" rather
than "failure". The verdict has to outlive that: under GitHub Actions (``GITHUB_ACTIONS``
set) the decision is also written as a workflow annotation (``::error`` for GENUINE,
``::warning`` for UNKNOWN) and appended to ``GITHUB_STEP_SUMMARY``, both of which persist
on a cancelled job, and ci.yml uploads the leg's log and JUnit BEFORE this step runs.

Requirements (mini-PRD)
- [if] the log names a failure absent from main's red set [then] the run is cancelled and
  the identity printed, [else stop] ✔︎ ✅ 🎯
- [if] every failure in the log is also red on main [then] nothing is cancelled and each is
  printed as known, [else stop] ✔︎ ✅ 🎯
- [if] no baseline is available, or fetching it is refused [then] exit 3, print UNKNOWN
  naming the cause, cancel nothing, [else stop] ✔︎ ✅ 🎯
- [if] the log names no failure at all (an infra-class death) [then] nothing is cancelled and
  that is said, because a cap kill is not a pull request defect, [else stop] ✔︎ ✅ 🎯
- [if] ``--dry-run`` [then] the decision is printed and `gh` is never called, [else stop]
  ✔︎ ✅ 🎯
- [if] running under GitHub Actions [then] GENUINE is an ``::error`` annotation and
  UNKNOWN a ``::warning``, both naming the leg, and both land in the step summary before
  any cancel, [else stop] ✔︎ ✅ 🎯
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# The watcher's parser, NOT a second copy of it. Both sides of this subtraction must speak
# one identity format, and until Thu 24 Sep 2026 they did not: this module stripped the
# `FAILED `/`ERROR ` prefix that scripts.ci_main_red's cached baseline keeps, so `failed -
# baseline` subtracted nothing and EVERY known trunk red read GENUINE. Armed, that cancels
# a run whose only failures are main's own. Importing the one parser makes the parity
# structural instead of a property two regexes have to keep agreeing on.
from scripts.ci_failure_ids import failed_identities

LINE_PREFIX = "[fast-cancel]"
EXIT_UNKNOWN = 3

__all__ = ["decide", "failed_identities", "main"]


def _baseline(main_red_json: Path | None) -> tuple[frozenset[str] | None, str]:
    """Main's red identities (None when no baseline can be had), and where they came from.

    Only ``identities`` is read, by attribute or key, so the watcher's record can grow
    (it gained ``measured_sha`` on Wed 16 Sep 2026: the commit the identities are ABOUT,
    which is usually older than ``main_sha``, since main's head rarely has a completed
    run). A baseline about an older commit is still a baseline; a refused cache
    (older payload without ``measured_sha``) surfaces here as the watcher's own error.
    """
    if main_red_json is not None:
        if not main_red_json.is_file():
            return None, f"--main-red-json {main_red_json} is not a file"
        identities = json.loads(main_red_json.read_text(encoding="utf-8"))["identities"]
        return frozenset(identities), f"--main-red-json {main_red_json}"
    try:
        from scripts.ci_main_red import cached_main_red  # optional, PR #3293
        from scripts.review_gh import TriageError
    except ImportError:
        return None, "scripts.ci_main_red not importable and no --main-red-json"
    try:
        return frozenset(cached_main_red().identities), "scripts.ci_main_red"
    except TriageError as refused:
        # A refused API read (Mon 21 Sep 2026: HTTP 403, the job lacked `checks: read`,
        # granted since) is a missing baseline, not a crash: a traceback under
        # continue-on-error reads as a successful step and hides that nothing was decided.
        # 291 failed-leg executions Thu 17 to Mon 21 Sep announced zero decisions this way.
        return None, f"scripts.ci_main_red could not fetch main's red set: {refused}"


def decide(failed: frozenset[str], baseline: frozenset[str] | None) -> tuple[str, frozenset[str]]:
    """One of UNKNOWN, NONE (no failure identity), KNOWN (all on main), GENUINE."""
    if baseline is None:
        return "UNKNOWN", frozenset()
    if not failed:
        return "NONE", frozenset()
    genuine = failed - baseline
    return ("GENUINE", genuine) if genuine else ("KNOWN", frozenset())


def _announce(level: str, title: str, body: str) -> None:
    """Persist a verdict past a cancelled job: annotation + step summary, Actions only."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    one_line = body.replace("\n", " ")
    print(f"::{level} title={title}::{one_line}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(f"### {title}\n\n{body}\n\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log", type=Path, required=True, help="the leg's captured pytest output")
    parser.add_argument("--run-id", required=True, help="GITHUB_RUN_ID to cancel")
    parser.add_argument(
        "--main-red-json",
        type=Path,
        default=None,
        help="main's red set in scripts.ci_main_red's cache shape; default imports it",
    )
    parser.add_argument("--dry-run", action="store_true", help="decide and print, never call gh")
    parser.add_argument("--leg", default="?", help="leg label for annotations (e.g. '2 of 4')")
    args = parser.parse_args(argv)

    failed = failed_identities(args.log.read_text(encoding="utf-8", errors="replace"))
    baseline, source = _baseline(args.main_red_json)
    verdict, genuine = decide(failed, baseline)
    if verdict == "UNKNOWN":
        print(f"{LINE_PREFIX} UNKNOWN: no main-red baseline ({source}); cancelling nothing")
        _announce(
            "warning",
            f"fast tier leg {args.leg}: cancel decision UNKNOWN",
            f"No main-red baseline on this head, so the {len(failed)} failing identities "
            f"could not be classified as known or genuine and nothing was cancelled ({source}). "
            "This is NOT evidence about the cancel logic.",
        )
        return EXIT_UNKNOWN
    if verdict == "NONE":
        print(f"{LINE_PREFIX} no failing test identity in the log (infra-class death?); "
              "cancelling nothing")
        return 0
    if verdict == "KNOWN":
        print(f"{LINE_PREFIX} every failure is already red on main; cancelling nothing:")
        for identity in sorted(failed):
            print(f"  known {identity}")
        return 0
    print(f"{LINE_PREFIX} GENUINE failure(s) not red on main; cancelling run {args.run_id}:")
    for identity in sorted(genuine):
        print(f"  genuine {identity}")
    mode = "DRY RUN, nothing cancelled" if args.dry_run else f"cancelling run {args.run_id}"
    _announce(
        "error",
        f"fast tier leg {args.leg}: GENUINE red ({mode})",
        "Failing on main-green identities:\n" + "\n".join(f"- `{i}`" for i in sorted(genuine)),
    )
    if args.dry_run:
        print(f"{LINE_PREFIX} dry run, gh not called")
        return 0
    subprocess.run(["gh", "run", "cancel", args.run_id], check=True, stdin=subprocess.DEVNULL)
    print(f"{LINE_PREFIX} cancelled run {args.run_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
