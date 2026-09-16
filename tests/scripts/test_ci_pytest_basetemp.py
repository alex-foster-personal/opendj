"""Every pytest invocation on a runner must use a basetemp under $RUNNER_TEMP.

MEASURED (Mon 14 Sep 2026, self-hosted host nucbox): every runner's TMPDIR (a
persistent disk dir, not wiped per job) held a `pytest-of-<user>/` with 63-85
numbered `pytest-N` basetemps, ~11-12.6 GB per runner, 289,704 MB across the
fleet. On one runner 83 of 85 dirs still carried pytest's `.lock` file. pytest
only prunes a numbered basetemp whose lock is gone or older than 3 days, and a
pytest process killed by the shard wall budget (exit 124/137, common on this
host) never removes its lock -- so each killed shard leaked ~140 MB for 3 days.

GitHub wipes `$RUNNER_TEMP` (`_work/_temp`) around every job, and
`pytest --basetemp=DIR` wipes DIR at session start, so pointing basetemp at a
path under RUNNER_TEMP fixes the class: no leak even on a kill.

This file pins the invariant, not a count of invocations: any pytest
invocation added to a workflow later, in whatever shape, must still carry a
`--basetemp` (or, for a `make` target that reaches pytest indirectly, a
`PYTEST_BASETEMP=`) resolving under `${RUNNER_TEMP:?...}` -- fail-loud, not a
silent default to /tmp.

Regression lines:
  - if a new pytest step omits --basetemp then it leaks into $TMPDIR again
  - if a step falls back to `${RUNNER_TEMP:-/tmp}` instead of `:?` then an
    unset RUNNER_TEMP silently writes to /tmp on a host where that outlives
    the job, instead of failing loud
  - if two pytest invocations in the same job share one basetemp dir then the
    second wipes the first's tmp files out from under it mid-job
  - if the Makefile's $(PYTEST) targets stop accepting PYTEST_BASETEMP then a
    workflow can pass the variable and have it silently ignored
  - if the Makefile joins PYTEST_BASETEMP and $@ with '/' then pytest's
    tmp_path setup errors on a freshly wiped RUNNER_TEMP (issue #3162)
    """

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
MAKEFILE = REPO_ROOT / "Makefile"

# ---------------------------------------------------------------------------
# Shell-ish tokenizing: split a `run:` block into command segments, joining
# backslash-newline continuations first so a multi-line invocation reads as
# one segment. This is not a real shell parser; it is precise enough for the
# invocation shapes this repo actually uses (verified below against every
# real workflow file and a synthetic control).
# ---------------------------------------------------------------------------
_CONTINUATION_RE = re.compile(r"\\\s*\n")
_SEGMENT_SPLIT_RE = re.compile(r"[\n;&|]+")

_DIRECT_PYTEST_RE = [
    re.compile(r"\.venv/bin/pytest\b"),
    re.compile(r"(?:python3?|\.venv/bin/python)\s+-m\s+pytest\b"),
    re.compile(r"\buv\s+run\b[^\n]*?(?:-m\s+pytest\b|\bpytest\b)"),
]
_MAKE_INVOCATION_RE = re.compile(r"^make\s+([A-Za-z0-9_.-]+)")
_MAKEFILE_BASETEMP_FLAG_RE = re.compile(
    r"pytest_basetemp_flag\s*=\s*\$\(if\s+\$\(PYTEST_BASETEMP\),"
    r"--basetemp=\$\(PYTEST_BASETEMP\)-\$@,\)"
)

# The flag itself must resolve under RUNNER_TEMP with the fail-loud `:?`
# form. `${RUNNER_TEMP:-/tmp}` (a silent default) does NOT satisfy this.
_DIRECT_BASETEMP_OK_RE = re.compile(r'--basetemp="\$\{RUNNER_TEMP:\?[^}]*\}')
_INDIRECT_BASETEMP_OK_RE = re.compile(r'PYTEST_BASETEMP="\$\{RUNNER_TEMP:\?[^}]*\}')


def _command_segments(run_text: str) -> list[str]:
    joined = _CONTINUATION_RE.sub(" ", run_text)
    return [seg.strip() for seg in _SEGMENT_SPLIT_RE.split(joined) if seg.strip()]


def _all_workflow_steps() -> list[tuple[str, str, str, dict]]:
    """Yield (workflow filename, job name, step name, step dict) for every step."""
    out: list[tuple[str, str, str, dict]] = []
    for path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_name, job in (doc.get("jobs") or {}).items():
            out.extend(
                (path.name, job_name, step.get("name") or "<unnamed>", step)
                for step in job.get("steps") or []
            )
    return out


def _pytest_invocations(run_text: str) -> list[tuple[str, str | None, str]]:
    """Return (kind, make_target_or_None, segment) for every pytest-shaped
    command segment in a `run:` block. kind is "direct" or "make"."""
    found: list[tuple[str, str | None, str]] = []
    for segment in _command_segments(run_text):
        make_match = _MAKE_INVOCATION_RE.match(segment)
        if make_match:
            found.append(("make", make_match.group(1), segment))
            continue
        if any(pattern.search(segment) for pattern in _DIRECT_PYTEST_RE):
            found.append(("direct", None, segment))
    return found


# ---------------------------------------------------------------------------
# Makefile: compute which targets reach a $(PYTEST) invocation, directly or
# through their prerequisite chain, so a `make <target>` step in a workflow
# can be checked against the real dependency graph instead of a hardcoded
# list that rots the moment the Makefile changes.
# ---------------------------------------------------------------------------
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*[:+?]?=")
_TARGET_RE = re.compile(r"^([A-Za-z0-9_.-]+)\s*:\s*(.*)$")


def _parse_makefile(text: str) -> tuple[dict[str, list[str]], dict[str, str]]:
    prereqs: dict[str, list[str]] = {}
    recipe_lines: dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in text.splitlines():
        if raw_line.startswith("\t"):
            if current is not None:
                recipe_lines.setdefault(current, []).append(raw_line[1:])
            continue
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip() or _ASSIGNMENT_RE.match(line):
            current = None
            continue
        match = _TARGET_RE.match(line)
        if match and match.group(1) != ".PHONY":
            current = match.group(1)
            prereqs.setdefault(current, []).extend(match.group(2).split())
            continue
        current = None
    return prereqs, {name: "\n".join(lines) for name, lines in recipe_lines.items()}


def _reaches_pytest(
    target: str,
    prereqs: dict[str, list[str]],
    recipes: dict[str, str],
    memo: dict[str, bool],
) -> bool:
    if target in memo:
        return memo[target]
    memo[target] = False  # cycle guard
    direct = "$(PYTEST)" in recipes.get(target, "")
    result = direct or any(
        _reaches_pytest(dep, prereqs, recipes, memo)
        for dep in prereqs.get(target, [])
        if dep in prereqs or dep in recipes
    )
    memo[target] = result
    return result


def _makefile_pytest_targets() -> set[str]:
    prereqs, recipes = _parse_makefile(MAKEFILE.read_text(encoding="utf-8"))
    memo: dict[str, bool] = {}
    return {t for t in prereqs if _reaches_pytest(t, prereqs, recipes, memo)}


def test_makefile_basetemp_flag_uses_hyphenated_target_suffix() -> None:
    """if PYTEST_BASETEMP/$@ is used then tmp_path setup fails on a wiped runner"""
    makefile_text = MAKEFILE.read_text(encoding="utf-8")
    assert _MAKEFILE_BASETEMP_FLAG_RE.search(makefile_text), (
        "pytest_basetemp_flag must join PYTEST_BASETEMP and $@ with '-', not '/', "
        "so pytest can create the basetemp on a freshly wiped RUNNER_TEMP"
    )
    assert "PYTEST_BASETEMP)/$@" not in makefile_text, (
        "nested PYTEST_BASETEMP/$@ basetemp paths require a parent directory "
        "pytest does not create (issue #3162)"
    )


def test_makefile_targets_reaching_pytest_forward_a_basetemp_override() -> None:
    """if a $(PYTEST) recipe line drops the override then a workflow's
    PYTEST_BASETEMP=... is silently ignored"""
    _, recipes = _parse_makefile(MAKEFILE.read_text(encoding="utf-8"))
    offenders = [
        (target, line)
        for target, recipe in recipes.items()
        for line in recipe.splitlines()
        if "$(PYTEST)" in line and "pytest_basetemp_flag" not in line
    ]
    assert not offenders, offenders


def test_every_direct_pytest_invocation_in_workflows_sets_basetemp() -> None:
    """if a direct pytest step omits --basetemp then it leaks tmp files again"""
    violations = []
    seen_files: set[str] = set()
    for workflow, job, step_name, step in _all_workflow_steps():
        run_text = step.get("run")
        if not run_text:
            continue
        for kind, _target, segment in _pytest_invocations(run_text):
            if kind != "direct":
                continue
            seen_files.add(workflow)
            if not _DIRECT_BASETEMP_OK_RE.search(segment):
                violations.append(f"{workflow} :: {job} :: {step_name}\n    {segment[:200]}")
    assert not violations, (
        "direct pytest invocation(s) missing "
        '--basetemp="${RUNNER_TEMP:?RUNNER_TEMP must be set}/...":\n'
        + "\n".join(violations)
    )
    # Positive control on the scanner itself: it must actually have found
    # something in each file known to invoke pytest directly, or this test
    # would pass vacuously by matching nothing.
    expected_files = {"ci.yml", "full-ci.yml", "macos-packaging.yml"}
    assert expected_files <= seen_files, (
        f"the scanner found no direct pytest invocation in {expected_files - seen_files}; "
        "it may have stopped matching a real invocation shape"
    )


def test_every_make_pytest_invocation_in_workflows_forwards_runner_temp() -> None:
    """if a workflow's `make <target>` step forgets PYTEST_BASETEMP then the
    Makefile's own pytest invocation still writes to the unmanaged default"""
    pytest_targets = _makefile_pytest_targets()
    violations = []
    seen_files: set[str] = set()
    for workflow, job, step_name, step in _all_workflow_steps():
        run_text = step.get("run")
        if not run_text:
            continue
        for kind, target, segment in _pytest_invocations(run_text):
            if kind != "make":
                continue
            if target not in pytest_targets:
                continue
            seen_files.add(workflow)
            if not _INDIRECT_BASETEMP_OK_RE.search(segment):
                violations.append(f"{workflow} :: {job} :: {step_name}\n    {segment[:200]}")
    assert not violations, (
        "`make` step(s) reaching a $(PYTEST) target without forwarding "
        'PYTEST_BASETEMP="${RUNNER_TEMP:?RUNNER_TEMP must be set}/...":\n'
        + "\n".join(violations)
    )
    expected_files = {"ci.yml", "macos-native-companion.yml", "release-check.yml"}
    assert expected_files <= seen_files, (
        f"the scanner found no make-mediated pytest invocation in {expected_files - seen_files}; "
        "it may have stopped matching a real invocation shape"
    )


# ---------------------------------------------------------------------------
# Controls. A check that has never been shown to fire on a real defect, in
# both directions, has not been validated -- see .claude/rules/verification.md.
# ---------------------------------------------------------------------------


def test_negative_control_missing_basetemp_is_reported() -> None:
    """The scanner must flag a pytest step that omits --basetemp: a synthetic
    workflow shaped exactly like a real one, minus the fix."""
    synthetic = yaml.safe_load(
        """
        jobs:
          test:
            runs-on: ubuntu-latest
            steps:
              - name: Run tests
                run: |
                  .venv/bin/pytest --collect-floor 10 tests/
              - name: Run tests via uv
                run: |
                  uv run --no-sync pytest tests/quality -q
        """
    )
    violations = []
    for job_name, job in synthetic["jobs"].items():
        for step in job["steps"]:
            for kind, _target, segment in _pytest_invocations(step["run"]):
                if kind == "direct" and not _DIRECT_BASETEMP_OK_RE.search(segment):
                    violations.append((job_name, step["name"]))
    assert violations == [
        ("test", "Run tests"),
        ("test", "Run tests via uv"),
    ], (
        "the scanner did not flag a synthetic pytest step missing --basetemp; "
        f"it is not exercising the real check. Found: {violations}"
    )


def test_negative_control_a_fixed_step_reports_clean() -> None:
    """The same synthetic step, fixed, must report no violation -- proving the
    check can also report a genuine PASS, not just always red."""
    synthetic = yaml.safe_load(
        """
        jobs:
          test:
            runs-on: ubuntu-latest
            steps:
              - name: Run tests
                run: |
                  .venv/bin/pytest --collect-floor 10 \\
                    --basetemp="${RUNNER_TEMP:?RUNNER_TEMP must be set}/pytest-basetemp-demo" \\
                    tests/
        """
    )
    step = synthetic["jobs"]["test"]["steps"][0]
    violations = [
        segment
        for kind, _target, segment in _pytest_invocations(step["run"])
        if kind == "direct" and not _DIRECT_BASETEMP_OK_RE.search(segment)
    ]
    assert violations == []


def test_control_does_not_false_positive_on_non_invocation_pytest_mentions() -> None:
    """`pytest` as a dependency name or in prose must not be misread as an
    invocation -- the two real shapes this repo actually contains, so a
    naive `"pytest" in run_text` scanner is refuted by this file's own
    workflows rather than by a contrived example."""
    synthetic = yaml.safe_load(
        """
        jobs:
          native:
            runs-on: macos-14
            steps:
              - name: Install release gate dependencies
                run: |
                  uv venv --python 3.11 .venv
                  uv pip install --python .venv/bin/python -r requirements.txt pytest
              - name: Post a status table
                run: |
                  cat > comment.md <<EOF
                  | 1 | full pytest suite incl. slow markers | PASS |
                  EOF
        """
    )
    found = []
    for job in synthetic["jobs"].values():
        for step in job["steps"]:
            found.extend(_pytest_invocations(step["run"]))
    assert found == [], (
        f"the scanner misread a non-invocation mention of 'pytest' as a real "
        f"invocation: {found}"
    )
