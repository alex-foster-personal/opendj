"""Every self-hosted job that checks out the repo runs the stale-install guard.

scripts/ci_stale_install_guard.py only protects a job that calls it (directly or
through scripts/ci_runner_preflight.sh). Review of PR #4251 found self-hosted jobs
that ran Python without it, so a stale wheel in the shared setup-python toolcache
could still shadow the checkout there. This test pins an INVARIANT over every
``.github/workflows/*.yml|yaml`` job rather than a list of jobs.

SCOPE: EVERY SELF-HOSTED JOB THAT CHECKS OUT THE REPO, not "every job seen to launch
Python". A trigger list of wrappers (make, just, uv, scripts/*.sh, *.py) is not
closed over what this repo actually runs: ``pnpm exec playwright test`` starts the
engine through Playwright's ``webServer`` (``uv run --no-sync python -m
apps.engine_core`` in apps/webui/frontend/playwright.config.ts and the boot-burst
config), so periodic-checks.yml's perf-bench jobs launch Python with no Python token
in any step. Once a job has the checkout, any wrapper can reach the project, and
resolving wrappers is an open-ended parser. The guard is cheap (stdlib, well under a
second) and runs on the host's system python3 when the job has no setup-python, so
guarding every checkout job costs almost nothing and cannot miss a wrapper.

THE RULES, per self-hosted job:
  R1 a job with an ``actions/checkout`` step runs the guard after the checkout and
     after its LAST ``actions/setup-python``/``astral-sh/setup-uv`` step. That guard
     inspects the interpreter the job ends up with, and a stale install fails the job
     however the Python was reached.
  R2 every step that launches Python, directly or through a wrapper this test can
     see, has a guard between the MOST RECENT setup-python/setup-uv step (if any) and
     itself. A job that guards one interpreter, switches with a second setup step,
     and launches again is red: every interpreter it uses is checked before use.
  R3 a job with NO checkout launches no Python at all, because the guard cannot run
     there. Today those jobs only call ``gh``.
  A guard with ``continue-on-error: true`` does not count.

WHAT R2 COUNTS AS A LAUNCH. A ``run:`` (shell comments ignored) naming ``python``,
``python3``, ``python3.N``, ``pytest``, ``uv``, ``uvx``, ``make``, ``just``, any
``scripts/...`` path or any ``*.py`` path, or a ``shell: python`` step or job
default. A repo shell script whose OWN text contains none of those tokens is not a
launch: that exempts the pre-guard ``scripts/ci_clean_untracked.sh`` every job runs.
The exemption is fail-closed. A script that calls another wrapper, names Python, or
cannot be found at the path the step uses still counts. Wrappers outside this list,
like pnpm reaching Python through Playwright, are covered by R1: the job still fails
at its guard.

HOSTED JOBS ARE EXEMPT. A job whose ``runs-on`` is only literal GitHub-hosted image
labels (``ubuntu-*``, ``macos-*``, ``windows-*``) gets a fresh VM with a pristine
toolcache, so no earlier job can have left a project install in it. Any other
``runs-on`` counts as self-hosted, including every ``${{ ... vars.CI_RUNS_ON_* }}``
switch: the repo variable routes it to the self-hosted pool, and a value this file
cannot read must not be assumed safe.

Regression lines:
  - if a self-hosted job that checks out the repo has no guard then broken
  - if a job's only guard runs before its last setup-python/setup-uv then broken
  - if a launch after a second setup-python with no guard in between is green then broken
  - if a guard after that second setup-python still reads red then broken
  - if make/just/uv/scripts/*.sh/*.py before the guard is not flagged then broken
  - if scripts/ci_clean_untracked.sh before the guard is flagged then broken
  - if a guard step with continue-on-error: true counts as a guard then broken
  - if a job on a literal ubuntu-latest runner is flagged then broken
  - if a vars.CI_RUNS_ON_* expression or a [self-hosted, ...] list is treated as hosted
    then broken
  - if the enumeration stops finding the jobs the reviews named then broken
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO / ".github" / "workflows"

GITHUB_HOSTED_LABEL = re.compile(r"^(?:ubuntu|macos|windows)-[\w.]+$")
LAUNCH_TOKEN = re.compile(
    r"(?<![\w.$/-])(?:python(?:3(?:\.\d+)?)?|pytest|uvx?|make|just)(?![\w-])"
    r"|(?<=/)(?:python(?:3(?:\.\d+)?)?|pytest)(?![\w-])"
    r"|(?<![\w.-])(?:\./)?scripts/[\w./-]+"
    r"|[\w./-]+\.py\b"
)
REPO_SHELL_SCRIPT = re.compile(r"(?:\./)?(?P<path>scripts/[\w./-]+\.sh)")
GUARD_CALL = re.compile(r"\bscripts/ci_(?:runner_preflight\.sh|stale_install_guard\.py)\b")
SETUP_ACTIONS = ("actions/setup-python@", "astral-sh/setup-uv@")
CHECKOUT_ACTION = "actions/checkout@"

#: Jobs the reviews of PR #4251 named: two that ran `python` unguarded, and one
#: that reaches Python only through wrappers (uv sync, then pnpm/Playwright). The
#: enumeration must find them, or it is walking the wrong files and every pass is
#: vacuous.
REVIEW_NAMED_JOBS = (
    "adr-check.yml:gate",
    "lockfile-check.yml:lock-metadata",
    "periodic-checks.yml:perf-bench",
)


# ---------------------------------------------------------------------------
# helpers


def _workflows() -> dict[str, dict]:
    paths = sorted(p for pat in ("*.yml", "*.yaml") for p in WORKFLOW_DIR.glob(pat))
    assert paths, f"no workflow files found under {WORKFLOW_DIR}"
    return {path.name: yaml.safe_load(path.read_text()) for path in paths}


def _shell_code(text: object) -> str:
    return "\n".join(
        line for line in str(text or "").splitlines() if not line.lstrip().startswith("#")
    )


def is_github_hosted(runs_on: object) -> bool:
    labels = runs_on if isinstance(runs_on, list) else [runs_on]
    return runs_on is not None and all(
        isinstance(label, str) and "${{" not in label and GITHUB_HOSTED_LABEL.match(label)
        for label in labels
    )


def _is_guard(step: dict) -> bool:
    return bool(GUARD_CALL.search(_shell_code(step.get("run")))) and not step.get(
        "continue-on-error"
    )


def _is_setup(step: dict) -> bool:
    return str(step.get("uses", "")).startswith(SETUP_ACTIONS)


def _is_checkout(step: dict) -> bool:
    return str(step.get("uses", "")).startswith(CHECKOUT_ACTION)


def _is_python_free_repo_script(token: str, step: dict) -> bool:
    """True only for a repo shell script found at the step's path whose own text names
    no launch token; anything else (unknown path, nested wrapper, Python) is a launch."""
    match = REPO_SHELL_SCRIPT.fullmatch(token)
    if match is None:
        return False
    script = REPO / step.get("working-directory", ".") / match["path"]
    return script.is_file() and not LAUNCH_TOKEN.search(_shell_code(script.read_text()))


def launch_tokens(step: dict, job: dict) -> list[str]:
    """What in this step can start a Python interpreter, per R2."""
    if _is_guard(step):
        return []
    tokens = [
        match.group(0)
        for match in LAUNCH_TOKEN.finditer(_shell_code(step.get("run")))
        if not _is_python_free_repo_script(match.group(0), step)
    ]
    default_shell = ((job.get("defaults") or {}).get("run") or {}).get("shell", "")
    shell = str(step.get("shell") or (default_shell if "run" in step else ""))
    return tokens + ["shell: python"] * shell.startswith("python")


def self_hosted_jobs(workflows: dict[str, dict]) -> dict[str, dict]:
    return {
        f"{name}:{job_id}": job
        for name, doc in workflows.items()
        for job_id, job in (doc.get("jobs") or {}).items()
        if "steps" in job and not is_github_hosted(job.get("runs-on"))
    }


def job_violations(job: dict) -> list[str]:
    steps = job["steps"]
    found: list[str] = []
    last_setup = -1
    last_guard = -1
    for i, step in enumerate(steps):
        if _is_setup(step):
            last_setup = i
        elif _is_guard(step):
            last_guard = i
        elif (tokens := launch_tokens(step, job)) and not last_setup < last_guard:
            since = f"setup step #{last_setup}" if last_setup >= 0 else "job start"
            found.append(
                f"R2 step #{i} ({step.get('name')!r}) launches {sorted(set(tokens))} "
                f"with no guard since {since}"
            )
    checkouts = [i for i, step in enumerate(steps) if _is_checkout(step)]
    if not checkouts:
        if found:
            found.append("R3 launches Python but never checks out the repo, so no guard can run")
        return found
    floor = max(last_setup, checkouts[0])
    if not any(_is_guard(step) for step in steps[floor + 1 :]):
        found.append(
            f"R1 checks out the repo but runs no guard after step #{floor} "
            f"({steps[floor].get('name') or steps[floor].get('uses')!r})"
        )
    return found


def violations(workflows: dict[str, dict]) -> dict[str, list[str]]:
    return {
        key: found
        for key, job in self_hosted_jobs(workflows).items()
        if (found := job_violations(job))
    }


def _without_guard(workflows: dict[str, dict], key: str) -> dict[str, dict]:
    name, job_id = key.split(":")
    mutated = copy.deepcopy(workflows)
    job = mutated[name]["jobs"][job_id]
    job["steps"] = [step for step in job["steps"] if not _is_guard(step)]
    return mutated


CHECKOUT = {"uses": "actions/checkout@abc"}
SETUP_PY = {"uses": "actions/setup-python@abc", "with": {"python-version": "3.11"}}
GUARD = {"name": "guard", "run": "scripts/ci_runner_preflight.sh python"}


def _launch(cmd: str) -> dict:
    return {"name": cmd, "run": cmd}


# ---------------------------------------------------------------------------
# the invariant


def test_every_self_hosted_checkout_job_runs_the_guard_before_each_interpreter() -> None:
    found = violations(_workflows())
    assert not found, (
        "self-hosted job(s) can use an interpreter the stale-install guard has not "
        "inspected; run scripts/ci_runner_preflight.sh <python|python3> after checkout "
        "and after each setup-python/setup-uv, before the next launch:\n"
        + "\n".join(f"  {key}: {why}" for key, whys in sorted(found.items()) for why in whys)
    )


def test_enumeration_finds_the_jobs_the_reviews_named() -> None:
    jobs = self_hosted_jobs(_workflows())
    missing = [key for key in REVIEW_NAMED_JOBS if key not in jobs]
    assert not missing, f"enumeration no longer sees {missing}; it is not reading the workflows"


def test_enumeration_exempts_real_hosted_jobs() -> None:
    workflows = _workflows()
    hosted = [
        f"{name}:{job_id}"
        for name, doc in workflows.items()
        for job_id, job in (doc.get("jobs") or {}).items()
        if is_github_hosted(job.get("runs-on"))
        and any(launch_tokens(step, job) for step in job.get("steps", []))
    ]
    assert hosted, "no hosted job launches Python, so the hosted exemption is untested"
    flagged = set(self_hosted_jobs(workflows)) & set(hosted)
    assert not flagged, f"hosted jobs were treated as self-hosted: {sorted(flagged)}"


# ---------------------------------------------------------------------------
# controls on the real workflow files: the check bites, and does not overshoot


@pytest.mark.parametrize("key", (*REVIEW_NAMED_JOBS, "ci.yml:frontend-build"))
def test_removing_a_guard_goes_red(key: str) -> None:
    found = violations(_without_guard(_workflows(), key))
    assert any(why.startswith("R1") for why in found.get(key, [])), found.get(key)


def test_an_unguarded_job_moved_to_a_hosted_runner_is_not_flagged() -> None:
    mutated = _without_guard(_workflows(), "adr-check.yml:gate")
    mutated["adr-check.yml"]["jobs"]["gate"]["runs-on"] = "ubuntu-latest"
    assert "adr-check.yml:gate" not in violations(mutated)


def test_a_guard_before_setup_python_goes_red() -> None:
    mutated = copy.deepcopy(_workflows())
    steps = mutated["adr-check.yml"]["jobs"]["gate"]["steps"]
    guard = next(i for i, step in enumerate(steps) if _is_guard(step))
    setup = next(i for i, step in enumerate(steps) if _is_setup(step))
    steps.insert(setup, steps.pop(guard))
    assert violations(mutated)["adr-check.yml:gate"]


def test_a_continue_on_error_guard_does_not_count() -> None:
    mutated = copy.deepcopy(_workflows())
    for step in mutated["adr-check.yml"]["jobs"]["gate"]["steps"]:
        if _is_guard(step):
            step["continue-on-error"] = True
    assert "adr-check.yml:gate" in violations(mutated)


# ---------------------------------------------------------------------------
# controls on built jobs: review r4125096309 (every interpreter switch)


def test_a_launch_after_a_second_unguarded_setup_goes_red() -> None:
    job = {
        "steps": [
            CHECKOUT,
            SETUP_PY,
            GUARD,
            _launch("python a.py"),
            SETUP_PY,
            _launch("python b.py"),
        ]
    }
    found = job_violations(job)
    assert any(why.startswith("R2 step #5") for why in found), found


def test_a_guard_after_the_second_setup_is_green() -> None:
    first = [CHECKOUT, SETUP_PY, GUARD, _launch("python a.py")]
    job = {"steps": [*first, SETUP_PY, GUARD, _launch("python b.py")]}
    assert job_violations(job) == []


# ---------------------------------------------------------------------------
# controls on built jobs: review r4125096296 (indirect launches)


@pytest.mark.parametrize(
    "cmd",
    [
        "make waveform-native-release-check",
        "just reqs",
        "uv sync --extra dev",
        "bash scripts/ci_venv.sh 3.11",
        "./scripts/ci_venv.sh 3.11",
        "scripts/some_future_wrapper.sh",
        "tools/render.py --out x",
    ],
)
def test_a_wrapper_before_the_guard_goes_red(cmd: str) -> None:
    job = {"steps": [CHECKOUT, SETUP_PY, _launch(cmd), GUARD]}
    assert any(why.startswith("R2 step #2") for why in job_violations(job))


@pytest.mark.parametrize("cmd", ["make x", "just reqs", "uv sync", "scripts/ci_venv.sh 3.11"])
def test_the_same_wrapper_after_the_guard_is_green(cmd: str) -> None:
    assert job_violations({"steps": [CHECKOUT, SETUP_PY, GUARD, _launch(cmd)]}) == []


def test_a_checkout_job_with_no_visible_python_still_needs_a_guard() -> None:
    job = {"steps": [CHECKOUT, _launch("pnpm exec playwright test")]}
    assert any(why.startswith("R1") for why in job_violations(job))
    assert job_violations({"steps": [CHECKOUT, GUARD, _launch("pnpm exec playwright test")]}) == []


def test_python_free_repo_script_before_the_guard_is_green() -> None:
    job = {"steps": [CHECKOUT, _launch("scripts/ci_clean_untracked.sh"), SETUP_PY, GUARD]}
    assert job_violations(job) == []


def test_a_job_without_checkout_may_not_launch_python() -> None:
    assert any(why.startswith("R3") for why in job_violations({"steps": [_launch("python3 -c 1")]}))
    assert job_violations({"steps": [_launch("gh issue list")]}) == []


# ---------------------------------------------------------------------------
# classification tables


@pytest.mark.parametrize(
    ("runs_on", "hosted"),
    [
        ("ubuntu-latest", True),
        ("macos-14", True),
        (["windows-latest"], True),
        ("${{ fromJSON(vars.CI_RUNS_ON_LINUX || '\"ubuntu-latest\"') }}", False),
        (["self-hosted", "linux"], False),
        (["ubuntu-latest", "agentbox"], False),
        (None, False),
    ],
)
def test_runner_classification(runs_on: object, hosted: bool) -> None:
    assert is_github_hosted(runs_on) is hosted


@pytest.mark.parametrize(
    ("step", "launches"),
    [
        ({"run": "python -m scripts.adr_check"}, True),
        ({"run": "set -e\npython3 scripts/x.py"}, True),
        ({"run": "uv run --no-sync pytest -q"}, True),
        ({"run": '"${GITHUB_WORKSPACE}/.venv/bin/python" x.py'}, True),
        ({"shell": "python", "run": "print(1)"}, True),
        ({"run": "scripts/ci_venv.sh 3.11"}, True),
        ({"run": "scripts/ci_clean_untracked.sh"}, False),
        ({"uses": "actions/setup-python@abc", "with": {"python-version": "3.11"}}, False),
        ({"run": "# python is not called here\necho ok"}, False),
        ({"run": "pnpm install --frozen-lockfile"}, False),
        ({"run": "scripts/ci_runner_preflight.sh python"}, False),
    ],
)
def test_launch_detection(step: dict, launches: bool) -> None:
    assert bool(launch_tokens(step, {})) is launches
