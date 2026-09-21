"""The `just pre-push` recipe has to keep running the four checks it exists for.

Four PRs produced five reds in one night, Thu 4 - Fri 5 Sep 2026, on four
mechanical faults a builder could have seen locally in seconds. #1177, #1178
and #1188 all changed a route or a model and pushed without regenerating BOTH
`apps/webui/openapi.json` and `apps/webui/frontend/src/lib/api-types.ts`; #1181
collided a new requirement id with one already on main; #1178 and #1181 also
crossed a file size ratchet. Each red cost a fixer agent and 20+ minutes of CI.
#1238 landed a fifth kind of red twice in one morning: step 4 used to run
`quality_gate --only size`, which never touches
frontend.import_cycles/max_fan_in/max_fan_out, so it passed here and reddened
CI's "Quality ratchet" job ~12 minutes later. Step 4 now runs the SAME
invocation that job runs (no `--only`), and this test's step 4 pattern was
updated to match -- see justfile `pre-push` and AGENTS.md.

`just pre-push` is the answer, so this test is the guard over the answer. The
failure mode it defends against is the ordinary one for a convenience recipe:
a step gets dropped, renamed, or quietly reordered during an unrelated edit,
the recipe still exits zero, and the class of red it was built to stop comes
straight back with nobody noticing the guard stopped guarding.

Order matters and is asserted, not just membership. The steps are arranged
cheapest-first so the commonest failure reports fastest, and step 2 must
regenerate openapi.json BEFORE step 3 reads it -- run the TS client generator
against a stale schema and it produces a stale client that looks freshly
generated.

tests/quality/test_gate_scope.py already parses this justfile for the same
reason; this is the same technique pointed at a different recipe.

Regression lines:
  - if the justfile stops carrying a `pre-push` recipe then broken
  - if any of the four steps (reqs check, openapi dump + diff, api:gen + diff,
    full quality gate) disappears from that recipe then broken
  - if the `scripts.debt_index --check` PR-head guard disappears then broken
    (OPS-17 criterion 5; do not require `--require-current`, that is main-side)
  - if step 4 goes back to a `--only` subset (e.g. `--only size`) instead of
    the full gate then broken
  - if the four steps stop appearing in that order then broken
  - if the openapi step stops using the shared `engine_openapi_dump` variable
    then it can drift away from what ci.yml dumps, so broken
  - if the recipe stops printing a PASS summary then a green run is
    indistinguishable from a run that did nothing, so broken
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
JUSTFILE = REPO_ROOT / "justfile"

RECIPE_NAME = "pre-push"

# (label, pattern) in the order the recipe must run them. Patterns match the
# command, not the surrounding comment or echo, so a step that is described but
# not run still fails.
REQUIRED_STEPS: list[tuple[str, str]] = [
    (
        "reviewer lease (open PR on current branch only)",
        r"scripts\.review_lease\s+check-branch",
    ),
    (
        "requirement ids and reqs.json drift",
        r"scripts\.build_reqs_json\s+--check",
    ),
    (
        "requirement-marker ratchets, the same suites the nucbox merge gate runs",
        r"pytest\s+.*tests/test_requirement_markers\.py\s+tests/test_requirement_intent_ratchet\.py\s+tests/quality/test_gate_scope\.py",
    ),
    (
        "dev-server registry launch.json drift",
        r"launch-json-check",
    ),
    (
        "tech debt index PR-head guard",
        r"scripts\.debt_index\s+--check",
    ),
    (
        "ADR line on gated paths",
        r"scripts\.adr_check\s+--base\s+origin/main",
    ),
    (
        "openapi.json regenerated from the engine and diffed",
        r"\{\{\s*engine_openapi_dump\s*\}\}.*apps/webui/openapi\.json",
    ),
    (
        "openapi.json compared against committed state",
        r"git diff --exit-code apps/webui/openapi\.json",
    ),
    (
        "TS client regenerated",
        r"pnpm run api:gen",
    ),
    (
        "TS client compared against committed state",
        r"git diff --exit-code src/lib/api-types\.ts",
    ),
    (
        "full quality gate, same invocation as ci.yml's Quality ratchet job",
        r"scripts\.quality_gate\s+--report\s+ops/quality/report\.md\s+--json\s+ops/quality/metrics\.json",
    ),
]


def _recipe_body(name: str) -> str:
    """Return the indented body of one just recipe, or fail loudly."""
    text = JUSTFILE.read_text(encoding="utf-8")
    lines = text.splitlines()
    starts = [
        i
        for i, line in enumerate(lines)
        if re.match(rf"^{re.escape(name)}(\s+[^:]*)?:", line)
    ]
    if not starts:
        pytest.fail(
            f"justfile has no `{name}` recipe. AGENTS.md tells every harness to "
            f"run `just {name}` before opening or updating a PR, so its absence "
            "is a broken instruction, not a missing convenience."
        )
    body: list[str] = []
    for line in lines[starts[0] + 1 :]:
        if line and not line[0].isspace():
            break
        body.append(line)
    return "\n".join(body)


def test_pre_push_recipe_exists() -> None:
    assert _recipe_body(RECIPE_NAME).strip(), "`just pre-push` has an empty body"


def test_pre_push_runs_the_four_contract_checks_in_order() -> None:
    body = _recipe_body(RECIPE_NAME)
    positions: list[int] = []
    for label, pattern in REQUIRED_STEPS:
        match = re.search(pattern, body)
        assert match is not None, (
            f"`just {RECIPE_NAME}` no longer runs: {label} (expected /{pattern}/). "
            "That is one of the four CI jobs this recipe exists to pre-empt."
        )
        positions.append(match.start())

    for (earlier, _), (later, _), a, b in zip(
        REQUIRED_STEPS, REQUIRED_STEPS[1:], positions, positions[1:],
        strict=False,
    ):
        assert a < b, (
            f"`just {RECIPE_NAME}` runs '{later}' before '{earlier}'. The order is "
            "load bearing: cheapest check first, and openapi.json must be "
            "regenerated before api:gen reads it."
        )


def test_pre_push_prints_a_pass_summary() -> None:
    body = _recipe_body(RECIPE_NAME)
    assert re.search(r"PASS", body), (
        f"`just {RECIPE_NAME}` prints no PASS line, so a clean run looks exactly "
        "like a run whose steps were all skipped."
    )
