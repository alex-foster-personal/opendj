"""One ruff version, or the local gate is lying about CI.

Three things in this repo run ruff, and until this test existed nothing made
them agree:

  - .pre-commit-config.yaml, the hook a developer feels on every commit
  - ops/quality/requirements.txt, the pin `make lint` and the `make quality`
    ratchet measure with
  - .github/workflows/release-check.yml, which used to `pip install ruff`
    unpinned into .venv, i.e. whatever shipped that morning

They drifted to v0.7.4 / 0.16.3 / floating, and the shape of the failure is
what makes this worth a test rather than a comment. Ruff's rule set is not
stable across minor versions, so an older hook does not merely lint less
strictly -- it cannot see whole rules. Measured on the tree at the time of
writing, 0.7.4 missed 113 findings 0.16.3 reports, including 69 RUF022
(unsorted __all__), 29 RUF046 and 15 RUF059. A single new unsorted __all__
therefore passed pre-commit clean and then pushed ruff.correctness past its
ops/quality/baseline.json allowance in CI, which is a red build bisected by
hand for a violation the author's own machine refused to print.

The blindness was mutual: 0.7.4 reported 31 UP038s against a rule 0.16.3 has
removed, and counted 260 UP007 where 0.16.3 splits the same code into 3 UP007
plus 257 UP045. So "clean locally" and "clean in CI" were not stricter and
looser versions of one question, they were different questions.

Pinning both to the same version is the fix; this test is what keeps it fixed,
because a routine `pre-commit autoupdate` re-opens the gap in one keystroke and
nothing else in the repo would notice.

Regression lines:
  - if .pre-commit-config.yaml's ruff-pre-commit rev stops matching the
    `ruff==` pin in ops/quality/requirements.txt then a clean pre-commit run
    no longer predicts the CI gate, so broken
  - if either file stops carrying a parseable ruff pin then this guard is
    measuring nothing, so broken
  - if a CI workflow installs ruff without a version then a third, floating
    ruff is back in the picture, so broken
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PRE_COMMIT = REPO_ROOT / ".pre-commit-config.yaml"
QUALITY_REQS = REPO_ROOT / "ops" / "quality" / "requirements.txt"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

# `rev: v0.16.3` on the ruff-pre-commit repo block. Anchored to the repo URL so
# a second hook repo gaining a `rev:` cannot satisfy this by accident.
_PRE_COMMIT_RUFF_REV = re.compile(
    r"repo:\s*https://github\.com/astral-sh/ruff-pre-commit\s*\n"
    r"(?:\s*#.*\n|\s*\n)*"
    r"\s*rev:\s*v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)",
)
_QUALITY_RUFF_PIN = re.compile(r"^ruff==(?P<version>[0-9]+\.[0-9]+\.[0-9]+)\s*$", re.MULTILINE)

# `pip install ... ruff` with no `==`. Word-bounded so `ruff-lsp` or a pinned
# `ruff==0.16.3` does not trip it.
_UNPINNED_RUFF_INSTALL = re.compile(r"pip install\b[^\n]*(?<![-\w])ruff(?![-\w=])")


def _pre_commit_ruff_version() -> str:
    match = _PRE_COMMIT_RUFF_REV.search(PRE_COMMIT.read_text())
    assert match, f"{PRE_COMMIT.name} has no parseable ruff-pre-commit rev"
    return match.group("version")


def _quality_ruff_version() -> str:
    match = _QUALITY_RUFF_PIN.search(QUALITY_REQS.read_text())
    assert match, f"{QUALITY_REQS.name} has no exact `ruff==` pin"
    return match.group("version")


def test_pre_commit_ruff_matches_the_quality_gate_pin() -> None:
    """The hook and the gate must run the identical ruff, not merely a close one."""
    hook_version = _pre_commit_ruff_version()
    gate_version = _quality_ruff_version()
    assert hook_version == gate_version, (
        f"ruff pin drift: .pre-commit-config.yaml pins v{hook_version} but "
        f"ops/quality/requirements.txt pins {gate_version}. Ruff adds and "
        "removes rules between minor versions, so these two disagreeing means "
        "a clean pre-commit run no longer predicts the CI ratchet. Bump both "
        "together, then re-record the baseline (ops/quality/README.md)."
    )


def test_no_workflow_installs_an_unpinned_ruff() -> None:
    """A floating `pip install ruff` in CI is a third version nobody chose."""
    offenders = sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in WORKFLOWS.glob("*.yml")
        if _UNPINNED_RUFF_INSTALL.search(path.read_text())
    )
    assert not offenders, (
        f"unpinned ruff install in {', '.join(offenders)}. The lint gate runs "
        "the ruff in ops/quality/requirements.txt via `uv run "
        "--with-requirements`; installing another one into the job venv adds a "
        "version that neither pin controls."
    )
