"""Wiring of the root Playwright spec selection in e2e.yml and durations-ledger.yml
(DEVOPS-28, TEST-BUDGET round 3).

`(private path)` covers the decision; this file pins the YAML
that feeds it and the step that consumes it, so the selection cannot silently stop
reaching the root step, and the Trunk quarantine verdict keeps reading the same report.

[if] the scope job selects specs and gates [then] the gate job runs exactly those, [else stop].

Regression lines:
  - [if] the scope job stops publishing `e2e_mode` and `e2e_specs`, or computes them on
    a non-PR event, or without the fetched map and the kill switch [then] fail.
  - [if] the root step stops appending the selection in affected mode, or appends anything
    in full mode, or loses the footprint reporter or E2E_FOOTPRINTS_DIR [then] fail.
  - [if] the root step's JUnit path or the verdict step after it changes [then] the Trunk
    quarantine verdict reads another report, so fail.
  - [if] a PR run or a red root run builds or uploads a footprint map [then] a partial run
    becomes the published map, so fail.
  - [if] durations-ledger.yml stops publishing the map to ci-data-e2e [then] fail.
  - [if] an always-run spec is missing from disk or ignored by the root config [then] the
    floor runs nothing, so fail.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from scripts.ci_e2e_selection import ALL_GATES, ALWAYS_RUN_NAME, GATES_AFTER_THE_MAP, STATIC_GATES, always_run_specs
from scripts.e2e_footprints_refresh import GATES

pytestmark = pytest.mark.requirement("DEVOPS-28")

REPO = Path(__file__).resolve().parents[2]
E2E = REPO / ".github" / "workflows" / "e2e.yml"
LEDGER = REPO / ".github" / "workflows" / "durations-ledger.yml"
FRONTEND = REPO / "apps" / "webui" / "frontend"
# The scope job resolves the event itself since Wed 4 Nov 2026 (ADR-NEW-e2e-after-ci-gate):
# `scope_event` is `pull_request` only for a workflow_run of a green pull-request head.
PR_IN_SCOPE = "steps.pr.outputs.scope_event == 'pull_request' && steps.decide.outputs.in_scope == 'true'"
MAIN_PUSH = "github.event_name == 'push' && github.ref == 'refs/heads/main'"
# The map's source runs: a push to main, or the hourly and nightly main-tip schedules,
# which concurrency never cancels mid-run.
MAIN_MAP_RUN = "(github.event_name == 'push' || github.event_name == 'schedule') && github.ref == 'refs/heads/main'"


def _jobs(path: Path = E2E) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"]


def _step(job: str, *, step_id: str | None = None, name: str | None = None, path: Path = E2E) -> dict:
    steps = [
        s for s in _jobs(path)[job]["steps"] if (step_id and s.get("id") == step_id) or (name and s.get("name") == name)
    ]
    assert len(steps) == 1, f"{job}: want one step id={step_id} name={name}, got {len(steps)}"
    return steps[0]


def test_the_scope_job_publishes_the_selection_computed_after_the_scope_decision() -> None:
    scope = _jobs()["scope"]
    assert scope["outputs"]["e2e_mode"] == "${{ steps.e2e-select.outputs.e2e_mode }}"
    assert scope["outputs"]["e2e_specs"] == "${{ steps.e2e-select.outputs.e2e_specs }}"
    assert scope["outputs"]["e2e_gates"] == "${{ steps.e2e-select.outputs.e2e_gates }}"
    ids = [s.get("id") for s in scope["steps"]]
    assert ids.index("decide") < ids.index("e2e-select")
    select = _step("scope", step_id="e2e-select")
    assert select["if"] == PR_IN_SCOPE
    assert select["env"]["CI_E2E_SPEC_SELECTION"] == "${{ vars.CI_E2E_SPEC_SELECTION }}"
    run = select["run"]
    assert "python3 -m scripts.ci_e2e_selection select" in run
    assert '--published "${RUNNER_TEMP:?RUNNER_TEMP must be set}/e2e-footprints/published.json"' in run
    assert '--out-dir "${RUNNER_TEMP:?RUNNER_TEMP must be set}/e2e-selection"' in run


def test_the_scope_job_fetches_the_published_map_without_failing_on_it() -> None:
    fetch = _step("scope", name="Resolve the published e2e footprint map (ci-data-e2e)")
    assert fetch["if"] == PR_IN_SCOPE
    assert fetch["continue-on-error"] is True and fetch["timeout-minutes"] == 1
    assert fetch["env"]["GH_TOKEN"] == "${{ github.token }}"
    run = fetch["run"]
    assert 'py="$(command -v python3)"' in run
    assert "scripts.e2e_footprints_refresh fetch" in run
    assert '--dest "${RUNNER_TEMP:?RUNNER_TEMP must be set}/e2e-footprints/published.json"' in run
    steps = _jobs()["scope"]["steps"]
    names = [s.get("name") or s.get("id") for s in steps]
    assert names.index(fetch["name"]) < names.index("Select the root Playwright specs this change set affects")


def test_the_root_step_appends_the_selection_only_in_affected_mode() -> None:
    step = _step("gate", step_id="root-playwright")
    env = step["env"]
    assert env["E2E_MODE"] == "${{ needs.scope.outputs.e2e_mode }}"
    assert env["E2E_SPECS"] == "${{ needs.scope.outputs.e2e_specs }}"
    assert env["E2E_FOOTPRINTS_DIR"] == "${{ runner.temp }}/e2e-footprints"
    assert env["PLAYWRIGHT_JUNIT_OUTPUT_FILE"] == "${{ runner.temp }}/junit-e2e-root.xml", (
        "the verdict reads this report"
    )
    run = step["run"]
    assert 'if [ "${E2E_MODE}" = "affected" ]; then' in run
    assert 'read -r -a spec_args <<< "${E2E_SPECS}"' in run
    assert "spec_args=()" in run
    assert (
        "pnpm test:e2e --output test-results/root-playwright "
        "--reporter=list,junit,./tests/e2e/support/footprint-reporter.ts "
        '${spec_args[@]+"${spec_args[@]}"}'
    ) in run
    assert "rm -rf" not in run, "the named gates before this step have already written gates/<name>/ into the directory"
    assert 'mkdir -p "${RUNNER_TEMP:?RUNNER_TEMP must be set}/e2e-footprints"' in run
    assert (FRONTEND / "tests" / "e2e" / "support" / "footprint-reporter.ts").is_file()
    assert "e2eFootprintsPlugin" in (FRONTEND / "vite.config.ts").read_text(encoding="utf-8")


def test_the_verdict_still_follows_the_root_step_and_the_map_steps_come_right_after_it() -> None:
    steps = _jobs()["gate"]["steps"]
    index = next(i for i, s in enumerate(steps) if s.get("id") == "root-playwright")
    assert steps[index + 1]["name"] == "Root Playwright verdict (Trunk quarantine applied)"
    assert steps[index + 1]["if"] == "always()"
    build = _step("gate", step_id="e2e-footprints")
    upload = _step("gate", name="Upload the root Playwright footprint map")
    # Right after the root suite, before the gates that follow it: on main the gate job hit its
    # 40 min timeout on 16 of 16 runs (Sun 8 to Mon 9 Nov), once a minute short of a map built
    # after the last gate (run 42598380583).
    assert steps.index(build) == index + 2, "the map is built straight after the root suite's verdict"
    assert steps.index(upload) == index + 3
    after = {gate for gate in GATES if steps.index(_step("gate", name=GATE_STEPS[gate][0])) > steps.index(build)}
    assert after == set(GATES_AFTER_THE_MAP), "a dev-server gate after the map is never in it; list it on purpose"
    # A red root run still measures every spec that passed; `build` records the rest
    # `unmeasured`, which the selector always runs. A cancelled or skipped suite builds nothing.
    assert build["if"] == (
        "${{ !cancelled() && "
        + MAIN_MAP_RUN
        + " && (steps.root-playwright.outcome == 'success' || steps.root-playwright.outcome == 'failure') }}"
    )
    assert build["continue-on-error"] is True
    assert ".venv/bin/python -m scripts.e2e_footprints_refresh build" in build["run"]
    assert '--sha "$GITHUB_SHA"' in build["run"]
    assert '--dir "${RUNNER_TEMP:?RUNNER_TEMP must be set}/e2e-footprints"' in build["run"]
    # `!cancelled()`: the root verdict step before them fails the job on a red run, and a step
    # `if:` with no status function is `success() && ...` (Sentry and Codex, PR #10552).
    assert upload["if"] == "${{ !cancelled() && " + MAIN_MAP_RUN + " && steps.e2e-footprints.outcome == 'success' }}"
    assert upload["uses"] == "./.github/actions/r2-artifact"
    assert upload["with"]["name"] == "e2e-spec-footprints"
    assert upload["with"]["include-hidden-files"] is True
    assert upload["with"]["path"].endswith("/.spec_footprints.json")
    assert upload["continue-on-error"] is True


def test_the_ledger_workflow_publishes_the_map_to_ci_data_e2e() -> None:
    build = _step("regenerate", name="Build the e2e footprint map from the newest main run", path=LEDGER)
    assert "scripts.e2e_footprints_refresh ledger" in build["run"]
    assert build["env"]["GH_TOKEN"] == "${{ github.token }}"
    publish = _step("regenerate", name="Publish the e2e footprint map to the ci-data-e2e branch", path=LEDGER)
    assert "scripts.e2e_footprints_refresh publish" in publish["run"]
    assert '[ -f "$map" ] ||' in publish["run"], "nothing fresh built keeps the published map"
    assert (
        _jobs(LEDGER)["regenerate"]["permissions"]["contents"] == "write"
        if "permissions" in _jobs(LEDGER)["regenerate"]
        else True
    )
    assert yaml.safe_load(LEDGER.read_text(encoding="utf-8"))["permissions"]["contents"] == "write"


def test_every_always_run_spec_exists_and_is_collected_by_the_root_config() -> None:
    specs = always_run_specs(REPO)
    assert specs, f"{ALWAYS_RUN_NAME} names no spec"
    config = (FRONTEND / "playwright.config.ts").read_text(encoding="utf-8")
    ignored = set(re.findall(r"'\*\*/([^']+\.spec\.ts)'", config))
    for spec in specs:
        assert (FRONTEND / spec).is_file(), spec
        assert Path(spec).name not in ignored, (
            f"{spec} is in the root config's testIgnore, so the floor would run nothing"
        )
        assert "[chromium]" not in spec and " " not in spec


#: Gate name -> its step in the gate job, and whether it also needs the production artifact.
GATE_STEPS: dict[str, tuple[str, bool]] = {
    "webkit": ("WebKit artifact gate (production build, named subset)", True),
    "deckload-smoke": ("Deck-load smoke (chromium + webkit, production build)", True),
    "rekordbox": ("Hermetic gate - rekordbox writeback disabled", False),
    "stretch": ("Hermetic gate - stretch quality smoke", False),
    "master12": ("Hermetic gate - master12-cue34 output topology", False),
    "comment-hotkey": ("Comment-hotkey gate - real backend, fixture library", False),
    "autoplay": ("AutoPlay stall-banner gate - real backend, fixture library", False),
    "headphone": ("Targeted performance-config gate - headphone probe and library panels", False),
    "library-jobs": ("Library jobs ordering (chromium, dry runner)", False),
    "preflight": ("Preflight boot gate + fresh-install onboarding (real backends)", False),
    "onboarding": ("Onboarding gauntlet (packaged engine, production build)", True),
    "cmdk-pick-race": ("Cmd K pick-race gate - real engine, fixture library", False),
    "cmdk-preview-cue": ("Cmd K cue-preview gate - real engine, fixture library", False),
}


#: Gates that also run on the smoke tier (TEST-BUDGET round 2, (private path)
#: SMOKE_STEPS): every other gate carries the tier guard before the selection clause.
SMOKE_TIER_GATES = frozenset({"deckload-smoke", "preflight", "master12"})
TIER_GUARD = "needs.scope.outputs.e2e_tier != 'smoke'"


def test_every_named_gate_step_runs_only_when_the_selection_names_it() -> None:
    assert set(GATE_STEPS) == set(ALL_GATES)
    for gate, (name, artifact) in GATE_STEPS.items():
        step = _step("gate", name=name)
        clause = f"(needs.scope.outputs.e2e_gates == '' || contains(needs.scope.outputs.e2e_gates, ' {gate} '))"
        prefix = "steps.frontend-artifact.outcome == 'success' && !cancelled()" if artifact else "!cancelled()"
        guards = [prefix] + ([] if gate in SMOKE_TIER_GATES else [TIER_GUARD]) + [clause]
        assert step["if"] == "${{ " + " && ".join(guards) + " }}", (gate, step["if"])


def test_every_dev_server_gate_records_its_footprint_and_the_static_ones_cannot() -> None:
    for gate, (name, _) in GATE_STEPS.items():
        step = _step("gate", name=name)
        run = str(step.get("run", ""))
        if gate in GATES:
            config, extra = GATES[gate]
            assert f"--config tests/e2e/{config}" in run, gate
            for argument in extra:
                assert argument in run, (gate, argument)
            assert "--reporter=list,./tests/e2e/support/footprint-reporter.ts" in run, gate
            assert step["env"]["E2E_FOOTPRINTS_DIR"] == f"${{{{ runner.temp }}}}/e2e-footprints/gates/{gate}", gate
            vite = (
                FRONTEND
                / "tests"
                / "e2e"
                / f"vite.{config.removeprefix('playwright.').removesuffix('.config.ts')}.config.ts"
            )
            assert "e2eFootprintsPlugin" in vite.read_text(encoding="utf-8"), vite
        else:
            assert gate in STATIC_GATES
            assert "footprint-reporter" not in run and "E2E_FOOTPRINTS_DIR" not in (step.get("env") or {}), gate
