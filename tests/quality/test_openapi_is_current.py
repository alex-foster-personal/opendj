"""The committed apps/webui/openapi.json is what the engine app dumps today.

Main run b011e2ca95 (Wed 4 Nov 2026) went red on ci.yml's "Contract drift - openapi.json" step:
#8314 added a 409 response to the cloudsync machine-rename route and did not regenerate the
committed contract. That step lives in the `contracts` job, which never runs on a pull request, so
the PR lane had no test that could see the drift. This module runs the step's own dump statement
(read from ci.yml, so the two cannot diverge) in a subprocess and diffs the result against the
committed file. tests/scripts/test_openapi_contract_snapshot.py pins the file's SHAPE; this pins
its CONTENT. scripts/ci_test_selection_rules.py pins this module at distance 0 when a change
touches the routes or the contract (OPENAPI_CURRENCY_GUARD).

Regression lines:
  - [if] a route changes and openapi.json is not regenerated [then] main's contract drift step goes red after merge, so fail, [else stop].
  - [if] ci.yml's dump step is renamed or reshaped [then] this guard would dump something else, so fail, [else stop].
"""

from __future__ import annotations

import difflib
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
OPENAPI = ROOT / "apps" / "webui" / "openapi.json"
CI = ROOT / ".github" / "workflows" / "ci.yml"
DRIFT_STEP = "Contract drift - openapi.json (armed at T0)"
DUMP_CALL = re.compile(r'python -c "(?P<code>[^"]+)" "\$RUNNER_TEMP/openapi\.ci\.json"')
DUMP_TIMEOUT_S = 120
DIFF_LINES_SHOWN = 40


def _dump_statement_from_ci() -> str:
    """The Python statement ci.yml's contract drift step dumps the schema with."""
    workflow = yaml.safe_load(CI.read_text(encoding="utf-8"))
    runs = [
        step["run"]
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if step.get("name") == DRIFT_STEP
    ]
    assert len(runs) == 1, f"expected one `{DRIFT_STEP}` step in {CI.name}, found {len(runs)}"
    match = DUMP_CALL.search(runs[0])
    assert match, f'`{DRIFT_STEP}` no longer runs `python -c "..." "$RUNNER_TEMP/openapi.ci.json"`'
    return match["code"]


def test_the_ci_dump_statement_is_found():
    """[if] ci.yml's dump step is renamed or reshaped [then] the currency check dumps nothing, so fail, [else stop]."""
    code = _dump_statement_from_ci()
    assert "create_app(cfg).openapi()" in code
    assert "sort_keys=True" in code


def test_the_committed_openapi_matches_a_fresh_dump(tmp_path: Path):
    """[if] a route changes without regenerating openapi.json [then] main's drift step goes red, so fail, [else stop]."""
    if importlib.util.find_spec("fastapi") is None:
        pytest.skip("fastapi is not installed in this environment, so the engine app cannot be dumped")
    out = tmp_path / "openapi.dump.json"
    # The CI step's env: MDT_LIBRARY_MODE=local. Lane-specific MDT_/MUSIC_DJ_ settings are dropped
    # so the dump sees what the step sees; the statement sets its own data dir.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MDT_", "MUSIC_DJ_"))}
    env["MDT_LIBRARY_MODE"] = "local"
    proc = subprocess.run(
        [sys.executable, "-c", _dump_statement_from_ci(), str(out)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=DUMP_TIMEOUT_S,
        check=False,
    )
    assert proc.returncode == 0, f"the engine OpenAPI dump failed (exit {proc.returncode}):\n{proc.stderr[-4000:]}"
    committed = OPENAPI.read_text(encoding="utf-8")
    fresh = out.read_text(encoding="utf-8")
    if committed != fresh:
        diff = list(
            difflib.unified_diff(
                committed.splitlines(), fresh.splitlines(), "committed openapi.json", "fresh dump", lineterm=""
            )
        )
        raise AssertionError(
            "apps/webui/openapi.json is stale against the engine app. Regenerate it with "
            "`just openapi-dump`, then `pnpm run api:gen` in apps/webui/frontend, and commit both.\n"
            + "\n".join(diff[:DIFF_LINES_SHOWN])
        )
