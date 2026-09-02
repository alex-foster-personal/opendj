"""The GUARD-10 tests full-ci.yml's coverage matrix can actually see.

test_frontend_typing_gate.py is `--ignore`d by full-ci.yml's coverage lane
because most of it shells out to apps/webui/frontend/node_modules/typescript,
which that job never installs. Round four of PR #731 review found the
consequence: with the whole file excluded, the coverage-matrix artifact
never collects a single `@pytest.mark.requirement("GUARD-10")` test, so
GUARD-10 reports permanently uncovered there even though ci.yml's `quality`
job runs the real gate on every push.

These two tests need nothing but tsconfig.json and quality_gate.py's own
Python-side HARD_ZERO set -- no subprocess, no node -- so they live here,
outside the ignore, and carry the marker the other file's tests lack.

Regression line:
  - if this file stops being collected by full-ci.yml's coverage lane then
    GUARD-10 goes back to reporting uncovered there, so broken
"""

from __future__ import annotations

import json

import pytest

from scripts import quality_gate as qg

pytestmark = pytest.mark.requirement("GUARD-10")

TSCONFIG = qg.FRONTEND / "tsconfig.json"

# Turned on by #724. Every one is a whole class of error the compiler was
# already able to find and was simply not asked about.
RATCHETED_FLAGS = (
    "noImplicitReturns",
    "noFallthroughCasesInSwitch",
    "exactOptionalPropertyTypes",
)


@pytest.fixture(scope="module")
def compiler_options() -> dict[str, object]:
    return json.loads(TSCONFIG.read_text(encoding="utf-8"))["compilerOptions"]


@pytest.mark.parametrize("flag", RATCHETED_FLAGS)
def test_tsconfig_keeps_the_ratcheted_strictness_flag(
    compiler_options: dict[str, object], flag: str
) -> None:
    """If a strictness flag is dropped then a whole error class goes unasked."""
    assert compiler_options.get(flag) is True, (
        f"if apps/webui/frontend/tsconfig.json stops setting {flag}: true then "
        "the #724 ratchet has been undone, so broken"
    )


def test_escape_hatch_metric_is_a_hard_zero() -> None:
    """If the metric is ratchetable then a baseline can license a suppression."""
    assert "frontend.ts_escape_hatches" in qg.HARD_ZERO, (
        "if frontend.ts_escape_hatches is not in HARD_ZERO then an allowance "
        "can absorb a compiler suppression, so broken"
    )
