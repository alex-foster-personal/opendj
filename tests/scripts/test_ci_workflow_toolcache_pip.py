"""No self-hosted workflow job pip-installs into the shared toolcache interpreter.

WHY. On a self-hosted runner the setup-python toolcache interpreter is SHARED by every
job on the host, so whatever one job pip-installs into it, every later job imports.
Mon 28 Sep 2026: a wheel installed that way shadowed the checkout on agentbox (PR
#4251, ADR PR #4214). scripts/ci_stale_install_guard.py detects the PROJECT package
only; it cannot see maturin, pyyaml or any other tool a job drops there.
periodic-checks.yml:perf-bench-waveform-render ran ``uv pip install 'maturin>=1.8,<2'``
with no ``--python``, which resolves to the job's ``.venv`` by default but to the first
python on PATH (the toolcache) whenever ``UV_SYSTEM_PYTHON`` is set in the runner's
environment. This test pins the invariant over every ``.github/workflows/*.yml|yaml``
job instead of fixing one line.

THE RULE, per self-hosted job (hosted/self-hosted classification is shared with
tests/scripts/test_ci_workflow_stale_install_coverage.py, so the two guards cannot
disagree about which jobs count): every pip install in a ``run:`` step (whole-line shell
comments ignored, backslash continuations joined) names a VIRTUALENV target explicitly.
Quoted text, trailing ``# ...`` comments and heredoc bodies are NOT masked, so a pip
install that only appears there is still flagged (fail-closed; see .planning/debt/4576.md).

  pip / pip3 / pip3.N install ...            flagged, unless spelled as a venv path
  python[3[.N]] -m pip install ...           flagged, unless the interpreter is a
                                             venv path (``.venv/bin/python -m pip``)
  uv pip install|sync ...                    flagged unless ``--python``/``-p`` names a
                                             venv path; ``--system`` is always flagged

A "venv path" contains a directory segment naming a virtualenv (``venv`` in any case,
so ``.venv/bin/python``, ``"$PWD/.venv/bin/python"``, ``$(WAVEFORM_CONSUMER_VENV)/bin/
python``) followed by ``/bin/``. The rule is FAIL-CLOSED: an activated venv with a bare
``pip``, a ``VIRTUAL_ENV`` set in ``env:``, or a venv directory whose name does not say
venv all read as violations. Spell the interpreter instead; it costs one flag.

HOSTED JOBS ARE EXEMPT. A literal ``ubuntu-*``/``macos-*``/``windows-*`` runner gets a
fresh VM and a pristine toolcache, so a bare ``pip install`` there cannot outlive the
job. Any ``${{ ... vars.CI_RUNS_ON_* }}`` switch counts as self-hosted.

BOUNDARY. Only workflow step text is read. Makefile and scripts/*.sh bodies are not
parsed here; the Makefile's own ``require-venv-py`` target refuses a non-venv ``PY`` at
run time, which is where the Mon 28 Sep 2026 install actually happened.

Regression lines:
  - if a self-hosted job running a bare ``pip install`` is green then broken
  - if ``python -m pip install`` / ``python3.11 -m pip install`` is green then broken
  - if ``uv pip install`` with no ``--python`` (the pre-fix maturin line) is green
    then broken
  - if ``uv pip install --python "$(command -v python)"`` (the Mon 28 Sep shape) is
    green then broken
  - if ``uv pip install --system`` is green even with a venv ``--python`` then broken
  - if ``uv pip install --python .venv/bin/python`` or ``.venv/bin/python -m pip
    install`` is flagged then broken
  - if a hosted ubuntu-latest job's bare ``pip install`` is flagged then broken
  - if that same job moved onto ``vars.CI_RUNS_ON_LINUX`` is still green then broken
  - if the scanner stops SEEING the maturin install it exists for then broken
"""

from __future__ import annotations

import copy
import re

import pytest

from tests.scripts.test_ci_workflow_stale_install_coverage import (
    _shell_code,
    _workflows,
    is_github_hosted,
    self_hosted_jobs,
)

#: A command word: start of text or after whitespace, a shell separator, a quote,
#: a subshell or a backtick. Keeps ``pipx``/``my-pip`` and ``--pip`` flags out.
_CMD_START = r"(?:^|(?<=[\s;&|(`\"']))"
_FLAGS = r"(?:\s+-{1,2}[\w-]+(?:=\S+)?)*"
_PIP_DIRECT = re.compile(
    _CMD_START
    + r"(?P<cmd>(?P<exe>[^\s;&|()`]*/)?pip(?:3(?:\.\d+)?)?(?![\w.-])"
    + _FLAGS
    + r"\s+install\b)"
)
_PIP_MODULE = re.compile(
    _CMD_START
    + r"(?P<cmd>(?P<exe>[\"']?[^\s;&|()`]*?python(?:3(?:\.\d+)?)?[\"']?)(?![\w.-])"
    + _FLAGS
    + r"\s+-m\s+pip"
    + _FLAGS
    + r"\s+install\b)"
)
_UV_PIP = re.compile(
    _CMD_START + r"(?P<cmd>uv" + _FLAGS + r"\s+pip\s+(?:install|sync)\b(?P<args>[^\n;&|]*))"
)
_UV_PYTHON_ARG = re.compile(r"(?<!\S)(?:--python|-p)(?:=|\s+)(?P<val>\"[^\"]*\"|'[^']*'|\S+)")
_UV_SYSTEM_ARG = re.compile(r"(?<!\S)--(?:system|break-system-packages)(?![\w-])")
#: Deliberately dumber than the scanner: any line naming pip and install/sync.
_CRUDE_PIP_LINE = re.compile(r"\bpip[\d.]*\b.*\b(?:install|sync)\b")
_VENV_PATH = re.compile(r"(?i)(?:^|/)[^/\s]*venv[^/\s]*/bin/")

WAVEFORM_JOB = "periodic-checks.yml:perf-bench-waveform-render"
WAVEFORM_STEP = "Install waveform native extension"
HOSTED_JOB = "periodic-checks.yml:duplicate-writer"


# ---------------------------------------------------------------------------
# helpers


def _is_venv_path(text: str | None) -> bool:
    return text is not None and bool(_VENV_PATH.search(text.strip("\"'")))


def pip_installs(run: object) -> list[tuple[str, bool]]:
    """Every pip install in a ``run:`` body, as (matched text, targets a venv)."""
    code = _shell_code(run).replace("\\\n", " ")
    found: list[tuple[int, str, bool]] = []
    claimed: list[range] = []

    def claim(match: re.Match[str], venv: bool) -> None:
        # `uv pip install` and `python -m pip install` both contain a `pip install`;
        # the most specific pattern claims the span so it is counted once.
        if not any(match.start("cmd") in span for span in claimed):
            claimed.append(range(match.start("cmd"), match.end("cmd")))
            found.append((match.start("cmd"), match["cmd"].strip(), venv))

    for match in _UV_PIP.finditer(code):
        args = match["args"]
        target = _UV_PYTHON_ARG.search(args)
        claim(
            match,
            target is not None and _is_venv_path(target["val"]) and not _UV_SYSTEM_ARG.search(args),
        )
    for match in _PIP_MODULE.finditer(code):
        claim(match, _is_venv_path(match["exe"]))
    for match in _PIP_DIRECT.finditer(code):
        claim(match, _is_venv_path(match["exe"]))
    return [(text, venv) for _, text, venv in sorted(found)]


def job_violations(job: dict) -> list[str]:
    return [
        f"step #{i} ({step.get('name')!r}) runs {text!r} against the toolcache interpreter"
        for i, step in enumerate(job.get("steps") or [])
        for text, venv in pip_installs(step.get("run"))
        if not venv
    ]


def violations(workflows: dict[str, dict]) -> dict[str, list[str]]:
    return {
        key: found
        for key, job in self_hosted_jobs(workflows).items()
        if (found := job_violations(job))
    }


def _job(workflows: dict[str, dict], key: str) -> dict:
    name, job_id = key.split(":")
    return workflows[name]["jobs"][job_id]


def _step(job: dict, name: str) -> dict:
    return next(step for step in job["steps"] if step.get("name") == name)


def _with_waveform_install(install_line: str) -> dict[str, dict]:
    """The real workflows, with the maturin install line replaced."""
    mutated = copy.deepcopy(_workflows())
    step = _step(_job(mutated, WAVEFORM_JOB), WAVEFORM_STEP)
    lines = step["run"].splitlines()
    idx = next(i for i, line in enumerate(lines) if "maturin>=" in line)
    lines[idx] = install_line
    step["run"] = "\n".join(lines) + "\n"
    return mutated


# ---------------------------------------------------------------------------
# the invariant


def test_no_self_hosted_job_pip_installs_into_the_toolcache() -> None:
    found = violations(_workflows())
    assert not found, (
        "self-hosted job(s) pip-install into an interpreter other jobs on the host "
        "share; name a venv target (uv pip install --python .venv/bin/python ...):\n"
        + "\n".join(f"  {key}: {why}" for key, whys in sorted(found.items()) for why in whys)
    )


# ---------------------------------------------------------------------------
# presence: the scanner sees the installs it exists for


def test_scanner_sees_the_maturin_install_as_venv_targeted() -> None:
    jobs = self_hosted_jobs(_workflows())
    assert WAVEFORM_JOB in jobs, f"{WAVEFORM_JOB} is no longer enumerated as self-hosted"
    installs = pip_installs(_step(jobs[WAVEFORM_JOB], WAVEFORM_STEP)["run"])
    maturin = [venv for text, venv in installs if "maturin" in text]
    assert maturin == [True], installs


def test_scanner_sees_every_pip_line_in_self_hosted_jobs() -> None:
    """Every self-hosted step line that a crude text search says mentions a pip
    install or sync is one the scanner parses. A scanner that reads nothing would
    leave the invariant above green for that reason alone; this cannot, because the
    crude search and the scanner have to agree line by line, and at least the
    maturin line has to be there."""
    candidates = [
        (key, line)
        for key, job in self_hosted_jobs(_workflows()).items()
        for step in job["steps"]
        for line in _shell_code(step.get("run")).replace("\\\n", " ").splitlines()
        if _CRUDE_PIP_LINE.search(line)
    ]
    assert any(key == WAVEFORM_JOB for key, _ in candidates), candidates
    unparsed = [(key, line.strip()) for key, line in candidates if not pip_installs(line)]
    assert not unparsed, f"pip lines the scanner does not parse: {unparsed}"


# ---------------------------------------------------------------------------
# controls on the real workflow files, direction 1: reverting the fix goes red


@pytest.mark.parametrize(
    "line",
    [
        "uv pip install 'maturin>=1.8,<2'",  # the pre-fix line
        "pip install 'maturin>=1.8,<2'",
        "python -m pip install 'maturin>=1.8,<2'",
        "uv pip install --system 'maturin>=1.8,<2'",
        "uv pip install --python \"$(command -v python)\" 'maturin>=1.8,<2'",
    ],
)
def test_reverting_the_maturin_install_goes_red(line: str) -> None:
    found = violations(_with_waveform_install("          " + line))
    assert WAVEFORM_JOB in found, f"{line!r} was not flagged"


# ---------------------------------------------------------------------------
# controls on the real workflow files, direction 2: no overshoot


def test_a_hosted_bare_pip_install_is_exempt_and_bites_once_self_hosted() -> None:
    workflows = copy.deepcopy(_workflows())
    job = _job(workflows, HOSTED_JOB)
    assert is_github_hosted(job.get("runs-on")), f"{HOSTED_JOB} is no longer hosted"
    job["steps"].append({"name": "bare pip", "run": "pip install --quiet pyyaml"})
    assert HOSTED_JOB not in violations(workflows)

    job["runs-on"] = "${{ fromJSON(vars.CI_RUNS_ON_LINUX || '\"ubuntu-latest\"') }}"
    assert HOSTED_JOB in violations(workflows)


def test_the_fixed_line_moved_to_other_venv_spellings_stays_green() -> None:
    for line in (
        "\"${GITHUB_WORKSPACE}/.venv/bin/python\" -m pip install 'maturin>=1.8,<2'",
        ".venv/bin/pip install 'maturin>=1.8,<2'",
        "uv pip install -p .venv/bin/python 'maturin>=1.8,<2'",
        "uv pip install --python=$PWD/.venv/bin/python 'maturin>=1.8,<2'",
    ):
        assert WAVEFORM_JOB not in violations(_with_waveform_install("          " + line)), line


# ---------------------------------------------------------------------------
# classification table


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        # toolcache (or host) interpreter: flagged
        ("pip install pyyaml", [False]),
        ("pip3 install --quiet pyyaml", [False]),
        ("pip3.11 install pyyaml", [False]),
        ("pip --disable-pip-version-check install x", [False]),
        ("python -m pip install x", [False]),
        ("python3 -m pip install --upgrade pip", [False]),
        ("python3.11 -m pip install x", [False]),
        ('"$pythonLocation/bin/python" -m pip install x', [False]),
        ("$pythonLocation/bin/pip install x", [False]),
        ("set -e; cd x && pip install -r r.txt", [False]),
        ("uv pip install x", [False]),
        ("uv pip sync requirements.txt", [False]),
        ("uv pip install --system x", [False]),
        ("uv pip install --python .venv/bin/python --system x", [False]),
        ('uv pip install --python "$(command -v python)" x', [False]),
        ("uv pip install --python python3.11 x", [False]),
        ("uv pip install \\\n  x", [False]),
        # a venv named explicitly: allowed
        ("uv pip install --python .venv/bin/python x", [True]),
        ("uv pip install --exact --upgrade --python .venv/bin/python -r r.txt", [True]),
        ('uv pip install --python "$venv/bin/python" -r r.txt', [True]),
        ("uv pip install \\\n  --python .venv/bin/python x", [True]),
        (".venv/bin/python -m pip install x", [True]),
        ('"${GITHUB_WORKSPACE}/.venv/bin/python" -m pip install x', [True]),
        ("$RUNNER_TEMP/tools-venv/bin/pip install x", [True]),
        # not a pip install at all
        ("pipx install maturin", []),
        ("pip download x", []),
        ("pip freeze", []),
        ("uv tool run maturin --version", []),
        ("uv sync --extra dev", []),
        ("# pip install x\necho ok", []),
        ("echo 'use --pip install'", []),
    ],
)
def test_pip_install_classification(run: str, expected: list[bool]) -> None:
    assert [venv for _, venv in pip_installs(run)] == expected
