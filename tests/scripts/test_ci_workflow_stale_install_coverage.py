"""Every self-hosted job that launches Python runs the stale-install guard FIRST.

scripts/ci_stale_install_guard.py only protects a job that calls it (directly or
through scripts/ci_runner_preflight.sh). Review of PR #4251 found jobs that ran
``python`` on a self-hosted runner and never called either, so a stale wheel in the
shared setup-python toolcache could still shadow the checkout there. This test pins
the INVARIANT rather than a list: it walks every ``.github/workflows/*.yml|yaml``
job, and each one that can land on a self-hosted runner and launches a Python
interpreter must run the guard before its first Python step and after every
``actions/setup-python`` step that precedes it (so the guard inspects the
interpreter the job then uses).

HOSTED JOBS ARE EXEMPT. A job whose ``runs-on`` is only literal GitHub-hosted image
labels (``ubuntu-*``, ``macos-*``, ``windows-*``) gets a fresh VM with a pristine
toolcache, so no earlier job can have left a project install in it. Any other
``runs-on`` counts as self-hosted, including every ``${{ ... vars.CI_RUNS_ON_* }}``
switch: the repo variable routes it to the self-hosted pool, and a value this file
cannot read must not be assumed safe.

WHAT "LAUNCHES PYTHON" MEANS here: a ``run:`` line (shell comments ignored) invoking
``python``, ``python3``, ``python3.N``, ``pytest`` or ``uv run``, or a step or job
default ``shell: python``. A script that launches Python internally without naming
it in the step is not seen; that is why the guard goes right after setup-python
rather than merely before the first detected launch.

Regression lines:
  - if a self-hosted job that runs python has no guard step then broken
  - if the guard step comes after the job's first python step then broken
  - if an actions/setup-python step sits between the guard and the first python step then broken
  - if a guard step with continue-on-error: true counts as a guard then broken
  - if a job on a literal ubuntu-latest runner is flagged then broken
  - if a vars.CI_RUNS_ON_* expression or a [self-hosted, ...] list is treated as hosted then broken
  - if the enumeration stops finding adr-check.yml:gate or lockfile-check.yml:lock-metadata
    then broken
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest
import yaml

WORKFLOW_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"

GITHUB_HOSTED_LABEL = re.compile(r"^(?:ubuntu|macos|windows)-[\w.]+$")
PYTHON_LAUNCH = re.compile(r"(?<![\w.$-])(?:python(?:3(?:\.\d+)?)?|pytest)(?![\w-])|\buv\s+run\b")
GUARD_CALL = re.compile(r"\bscripts/ci_(?:runner_preflight\.sh|stale_install_guard\.py)\b")
SETUP_PYTHON = "actions/setup-python@"

#: The two jobs PR #4251's review named as unguarded. The enumeration must find
#: them, or it is walking the wrong files (or none) and every pass is vacuous.
REVIEW_NAMED_JOBS = ("adr-check.yml:gate", "lockfile-check.yml:lock-metadata")


# ---------------------------------------------------------------------------
# helpers


def _workflows() -> dict[str, dict]:
    paths = sorted(p for pat in ("*.yml", "*.yaml") for p in WORKFLOW_DIR.glob(pat))
    assert paths, f"no workflow files found under {WORKFLOW_DIR}"
    return {path.name: yaml.safe_load(path.read_text()) for path in paths}


def _shell_code(run: object) -> str:
    return "\n".join(
        line for line in str(run or "").splitlines() if not line.lstrip().startswith("#")
    )


def is_github_hosted(runs_on: object) -> bool:
    labels = runs_on if isinstance(runs_on, list) else [runs_on]
    return runs_on is not None and all(
        isinstance(label, str) and "${{" not in label and GITHUB_HOSTED_LABEL.match(label)
        for label in labels
    )


def _runs_guard(step: dict) -> bool:
    return bool(GUARD_CALL.search(_shell_code(step.get("run")))) and not step.get(
        "continue-on-error"
    )


def _launches_python(step: dict, job: dict) -> bool:
    default_shell = ((job.get("defaults") or {}).get("run") or {}).get("shell", "")
    shell = str(step.get("shell") or (default_shell if "run" in step else ""))
    return shell.startswith("python") or bool(PYTHON_LAUNCH.search(_shell_code(step.get("run"))))


def self_hosted_python_jobs(workflows: dict[str, dict]) -> dict[str, dict]:
    """Every job that can land on a self-hosted runner and launches Python."""
    return {
        f"{name}:{job_id}": job
        for name, doc in workflows.items()
        for job_id, job in (doc.get("jobs") or {}).items()
        if "steps" in job
        and not is_github_hosted(job.get("runs-on"))
        and any(_launches_python(step, job) and not _runs_guard(step) for step in job["steps"])
    }


def guard_violation(job: dict) -> str | None:
    """Why this job's guard does not protect its first Python launch, or None."""
    steps = job["steps"]
    first_python = next(
        i for i, step in enumerate(steps) if _launches_python(step, job) and not _runs_guard(step)
    )
    guards = [i for i, step in enumerate(steps[:first_python]) if _runs_guard(step)]
    if not guards:
        return (
            f"no guard step before its first Python step #{first_python} "
            f"({steps[first_python].get('name')!r})"
        )
    last_setup = max(
        (i for i, step in enumerate(steps[:first_python]) if SETUP_PYTHON in step.get("uses", "")),
        default=-1,
    )
    if guards[-1] < last_setup:
        return (
            f"its guard (step #{guards[-1]}) runs before actions/setup-python "
            f"(step #{last_setup}), so it inspects an interpreter the job does not use"
        )
    return None


def violations(workflows: dict[str, dict]) -> dict[str, str]:
    return {
        key: why
        for key, job in self_hosted_python_jobs(workflows).items()
        if (why := guard_violation(job)) is not None
    }


def _without_guard(workflows: dict[str, dict], name: str, job_id: str) -> dict[str, dict]:
    mutated = copy.deepcopy(workflows)
    job = mutated[name]["jobs"][job_id]
    job["steps"] = [step for step in job["steps"] if not _runs_guard(step)]
    return mutated


# ---------------------------------------------------------------------------
# the invariant


def test_every_self_hosted_python_job_runs_the_guard_first() -> None:
    found = violations(_workflows())
    assert not found, (
        "self-hosted job(s) launch Python without first running "
        "scripts/ci_runner_preflight.sh (which runs scripts/ci_stale_install_guard.py) "
        "after setup-python; a stale music-dj-tools install in the shared toolcache "
        "would shadow the checkout there:\n"
        + "\n".join(f"  {key}: {why}" for key, why in sorted(found.items()))
    )


def test_enumeration_finds_the_jobs_the_review_named() -> None:
    jobs = self_hosted_python_jobs(_workflows())
    missing = [key for key in REVIEW_NAMED_JOBS if key not in jobs]
    assert not missing, f"enumeration no longer sees {missing}; it is not reading the workflows"


def test_enumeration_exempts_real_hosted_python_jobs() -> None:
    workflows = _workflows()
    hosted_python = [
        f"{name}:{job_id}"
        for name, doc in workflows.items()
        for job_id, job in (doc.get("jobs") or {}).items()
        if is_github_hosted(job.get("runs-on"))
        and any(_launches_python(step, job) for step in job.get("steps", []))
    ]
    assert hosted_python, "no hosted Python job exists, so the hosted exemption is untested"
    flagged = set(self_hosted_python_jobs(workflows)) & set(hosted_python)
    assert not flagged, f"hosted jobs were treated as self-hosted: {sorted(flagged)}"


# ---------------------------------------------------------------------------
# controls: the check bites in both directions, on the real workflow files


@pytest.mark.parametrize("key", REVIEW_NAMED_JOBS)
def test_removing_a_guard_goes_red(key: str) -> None:
    name, job_id = key.split(":")
    found = violations(_without_guard(_workflows(), name, job_id))
    assert key in found and "no guard step" in found[key], found


def test_an_unguarded_job_moved_to_a_hosted_runner_is_not_flagged() -> None:
    mutated = _without_guard(_workflows(), "adr-check.yml", "gate")
    mutated["adr-check.yml"]["jobs"]["gate"]["runs-on"] = "ubuntu-latest"
    assert "adr-check.yml:gate" not in violations(mutated)


def test_a_guard_after_the_first_python_step_goes_red() -> None:
    mutated = copy.deepcopy(_workflows())
    steps = mutated["adr-check.yml"]["jobs"]["gate"]["steps"]
    guard = next(i for i, step in enumerate(steps) if _runs_guard(step))
    steps.append(steps.pop(guard))
    assert "no guard step" in violations(mutated)["adr-check.yml:gate"]


def test_a_guard_before_setup_python_goes_red() -> None:
    mutated = copy.deepcopy(_workflows())
    steps = mutated["adr-check.yml"]["jobs"]["gate"]["steps"]
    guard = next(i for i, step in enumerate(steps) if _runs_guard(step))
    setup = next(i for i, step in enumerate(steps) if SETUP_PYTHON in step.get("uses", ""))
    steps.insert(setup, steps.pop(guard))
    assert "before actions/setup-python" in violations(mutated)["adr-check.yml:gate"]


def test_a_continue_on_error_guard_does_not_count() -> None:
    mutated = copy.deepcopy(_workflows())
    for step in mutated["adr-check.yml"]["jobs"]["gate"]["steps"]:
        if _runs_guard(step):
            step["continue-on-error"] = True
    assert "adr-check.yml:gate" in violations(mutated)


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
        ({"uses": "actions/setup-python@abc", "with": {"python-version": "3.11"}}, False),
        ({"run": "# python is not called here\necho ok"}, False),
        ({"run": "uv pip install --python-preference only-system x"}, False),
    ],
)
def test_python_launch_detection(step: dict, launches: bool) -> None:
    assert _launches_python(step, {}) is launches
