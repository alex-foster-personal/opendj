"""One red Playwright step must not silence the independent answers of the others.

Two properties, both of which the nightly ``extended`` job in ``e2e.yml`` was
violating at once (issue #913):

1. **Guard.** A GitHub Actions step defaults to ``if: success()``, so the first
   failing step SKIPS every step after it. ``extended`` opened with the FULL
   ``webkit-deckload`` suite, which is red BY DESIGN -- it is the no-grep run
   whose whole purpose is keeping the tests quarantined under #637/#680
   visible. So it failed every night, and desktop setup, play analytics,
   stretch artifact, stems progress and midi-map resync were SKIPPED on both
   nightly runs that had ever happened (2 of 2, structural rather than
   ordering luck). Those five configs are referenced nowhere else in CI, so the
   suites had never executed there at all. The tier-1 ``gate`` job already
   carried ``${{ !cancelled() }}`` on every one of its Playwright steps for
   exactly this reason; the guard was simply never applied to the other job.

2. **Distinct output directories.** Every Playwright config defaults to the
   same ``test-results/``, and Playwright clears its ``outputDir`` at the start
   of each run. With the guard in place, a later GREEN step deletes an earlier
   FAILED step's trace before the single end-of-job ``Upload failure artifacts``
   can read it. The reason is written up at ``e2e.yml`` on the tier-1a step
   (flagged on review, PR #860), and every ``gate`` step honours it; the
   ``extended`` steps mostly did not. Neither property could bite before the
   guard landed, because nothing after the first red step ever ran.

Regression lines:
  - if an e2e Playwright step drops its ``!cancelled()`` guard then a red suite
    above it skips it, and that suite's answer is lost for the whole run
  - if an e2e Playwright step drops ``--output test-results/<slug>`` then a
    later green step clears its output dir before the end-of-job upload reads
    it, so a failure ships with no trace
  - if two e2e Playwright steps name the SAME ``--output`` dir then the second
    run clears the first's traces exactly as if neither had set one
  - if the nightly job stops wiring one of the four never-run configs then this
    file's guard assertions pass vacuously over a suite that no longer runs
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E = REPO_ROOT / ".github" / "workflows" / "e2e.yml"

#: A step that RUNS a Playwright suite. Deliberately narrower than "mentions
#: playwright": it excludes the version probe, the browser install and
#: ``scripts/playwright_presence_check.py``, none of which produce a suite
#: result and none of which name an output directory.
RUNS_SUITE = re.compile(r"(?:playwright test|pnpm test:e2e)")
OUTPUT_DIR = re.compile(r"--output\s+(\S+)")
GUARD = "!cancelled()"

#: The four configs #913 measured as having never run in CI, plus midi-maps.
#: Named individually so a deleted step fails this file loudly instead of
#: leaving the guard assertions vacuously green over nothing.
NEVER_RUN_SUITES = (
    "playwright.desktop-setup.config.ts",
    "playwright.play-analytics.config.ts",
    "playwright.stretch-artifact.config.ts",
    "playwright.stems.config.ts",
)

NIGHTLY_JOB = "extended"


def _suite_steps(job: str) -> list[dict]:
    """Every step of ``job`` whose ``run`` invokes a Playwright suite."""
    steps = yaml.safe_load(E2E.read_text(encoding="utf-8"))["jobs"][job]["steps"]
    return [step for step in steps if RUNS_SUITE.search(step.get("run") or "")]


def _survives_an_upstream_failure(step: dict) -> bool:
    """True when the step's condition still holds after an earlier step failed.

    ``!cancelled()`` is the repo's idiom for this. The default (no ``if``) and
    ``success()`` both resolve false once anything upstream is red, which is the
    behaviour under test.
    """
    return GUARD in str(step.get("if") or "")


# ---------------------------------------------------------------------------
# Controls: prove the probe can see a suite step, and can say no about a guard.
# ---------------------------------------------------------------------------


def test_the_probe_finds_the_suite_steps() -> None:
    """if the probe stops matching suite steps then every guard assertion below passes over nothing"""
    gate, nightly = _suite_steps("gate"), _suite_steps(NIGHTLY_JOB)
    assert len(gate) >= 8, f"gate suite steps: {[s.get('name') for s in gate]}"
    assert len(nightly) >= 8, f"nightly suite steps: {[s.get('name') for s in nightly]}"


def test_the_guard_probe_can_answer_no() -> None:
    """if the guard probe returns true for an unguarded step then it cannot detect the defect"""
    assert _survives_an_upstream_failure({}) is False, "a step with no `if` runs on the default success()"
    assert _survives_an_upstream_failure({"if": "success()"}) is False, "success() is the failing default"
    assert _survives_an_upstream_failure({"if": "${{ !cancelled() }}"}) is True, "the guard itself must read true"
    assert _survives_an_upstream_failure({"if": "always() && !cancelled()"}) is True, "always() alone outlives a cancel"


def test_the_four_never_run_suites_are_wired_into_the_nightly_job() -> None:
    """if a step is deleted rather than guarded then the guard assertions below are vacuously green"""
    runs = "\n".join(step.get("run") or "" for step in _suite_steps(NIGHTLY_JOB))
    absent = [config for config in NEVER_RUN_SUITES if config not in runs]
    assert not absent, f"nightly job no longer runs these configs anywhere: {absent}"


# ---------------------------------------------------------------------------
# The two properties.
# ---------------------------------------------------------------------------


def test_every_e2e_suite_step_survives_an_upstream_failure() -> None:
    """if a suite step drops its guard then a red suite above it skips it and its answer is lost"""
    unguarded = [
        f"{job}/{step.get('name')}"
        for job in ("gate", NIGHTLY_JOB)
        for step in _suite_steps(job)
        if not _survives_an_upstream_failure(step)
    ]
    assert not unguarded, (
        "Playwright steps behind the default success() condition, so a red suite "
        f"above them SKIPS them instead of running them: {unguarded}"
    )


def test_every_e2e_suite_step_writes_to_its_own_output_dir() -> None:
    """if two suite steps share an output dir then the later run clears the earlier one's traces"""
    owner: dict[str, str] = {}
    missing: list[str] = []
    reused: list[str] = []
    for job in ("gate", NIGHTLY_JOB):
        for step in _suite_steps(job):
            where = f"{job}/{step.get('name')}"
            dirs = OUTPUT_DIR.findall(step.get("run") or "")
            if len(dirs) != 1:
                missing.append(f"{where}: {dirs}")
                continue
            if dirs[0] in owner:
                reused.append(f"{dirs[0]}: {owner[dirs[0]]} and {where}")
                continue
            owner[dirs[0]] = where
    assert not missing, (
        "Playwright steps that do not name exactly one --output dir, so they write into the "
        f"shared default test-results/ and a later step clears them: {missing}"
    )
    assert not reused, f"one --output dir claimed by two steps: {reused}"
