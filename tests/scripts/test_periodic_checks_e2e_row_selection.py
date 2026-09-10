"""Regression guard for the periodic-checks e2e-row selection in
``.github/workflows/periodic-checks.yml`` (DEVOPS-05).

Runs the workflow's OWN `e2e_line` selection script (extracted from the
file, not a copy) against a stubbed `gh` on PATH, so a future edit that
drifts from what actually executes cannot pass silently. See review finding
r3974766506: without `--status completed`, `--limit 1` can select the
04:17 scheduled run while it is still queued or running, whose null
conclusion rendered as "in progress" -- and the NEXT window's `--limit 1`
then moves on to a newer scheduled run and never revisits it. A run that
later fails after being reported "in progress" was never seen again by any
later window, exactly the silent-failure shape this row exists to catch.

The stub `gh` does not re-implement GitHub's own server-side filtering
(that is GitHub's contract, not this repo's); it logs its argv so a test can
assert `--status completed` was actually requested, then pipes a
test-supplied JSON payload -- standing in for whatever GitHub already
filtered server-side -- through the REAL `--jq` filter text extracted from
the workflow, so the downstream formatting is exercised for real rather
than asserted against a hand-typed expected string.
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "periodic-checks.yml"


def _extract_e2e_line_script() -> str:
    """Pull the `if e2e_line="$(gh run list ...)"; then ... fi` block out of
    the `report` job's `run:` script."""
    text = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r'( *if e2e_line="\$\(gh run list.*?\n *fi\n)', text, re.DOTALL)
    assert match, "could not locate the e2e_line selection block in periodic-checks.yml"
    return match.group(1)


_STUB_GH = """#!/usr/bin/env bash
echo "$@" >> "$STUB_LOG"
if [ -n "${STUB_FAIL:-}" ]; then
  echo "$STUB_FAIL" >&2
  exit 1
fi
jq_filter=""
prev=""
for a in "$@"; do
  if [ "$prev" = "--jq" ]; then jq_filter="$a"; fi
  prev="$a"
done
printf '%s' "$STUB_JSON" | jq -r "$jq_filter"
"""


def _run_with_stub_gh(
    tmp_path: Path, *, json_payload: list[dict] | None = None, fail: str | None = None
) -> tuple[str, str]:
    """Run the extracted script against the stub `gh` above and return
    ``(e2e_line, argv_log)``."""
    stub = tmp_path / "gh"
    stub.write_text(_STUB_GH, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    script = f'{_extract_e2e_line_script()}\nprintf "%s" "$e2e_line"\n'
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["STUB_LOG"] = str(tmp_path / "gh.argv")
    env["STUB_JSON"] = json.dumps(json_payload if json_payload is not None else [])
    if fail is not None:
        env["STUB_FAIL"] = fail
    proc = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    argv_path = tmp_path / "gh.argv"
    argv_log = argv_path.read_text(encoding="utf-8") if argv_path.exists() else ""
    return proc.stdout, argv_log


def test_the_query_restricts_to_completed_runs(tmp_path: Path) -> None:
    """The stub `gh` must be invoked with `--status completed`: without it,
    an in-flight 04:17 run can be selected and then abandoned once a later
    window's `--limit 1` moves on to a newer scheduled run."""
    _, argv = _run_with_stub_gh(tmp_path, json_payload=[])
    assert "--status completed" in argv, argv


def test_the_query_fetches_more_than_the_single_newest_run(tmp_path: Path) -> None:
    """r3974912528: the stub `gh` does not re-implement GitHub's own
    `--limit` filtering (see module docstring), so the newest-failure-wins
    behavior alone cannot catch a regression back to `--limit 1` -- a
    payload-based test would still pass no matter which value is on the
    command line, since the stub returns whatever payload the test hands it
    regardless of `--limit`. Asserting the argv directly is what actually
    guards it: `--limit 1` can only ever see ONE completed run per window,
    so an older failure sitting behind a newer pass would never even be
    fetched, let alone compared."""
    _, argv = _run_with_stub_gh(tmp_path, json_payload=[])
    assert "--limit 10" in argv, argv


def test_a_completed_runs_conclusion_is_reported_verbatim(tmp_path: Path) -> None:
    """The ordinary case: GitHub's own `--status completed` filtering has
    already dropped a still-running newer run, leaving the last COMPLETED
    one -- its real conclusion, not a stale "in progress" placeholder, ends
    up in `e2e_line`."""
    e2e_line, _ = _run_with_stub_gh(
        tmp_path,
        json_payload=[
            {
                "conclusion": "failure",
                "createdAt": "2026-09-08T04:17:00Z",
                "url": "https://example/runs/1",
            }
        ],
    )
    assert e2e_line == "failure (2026-09-08T04:17:00Z) https://example/runs/1"


def test_an_older_failure_is_not_masked_by_a_newer_pass(tmp_path: Path) -> None:
    """r3974912528: two nightly runs can both complete between two windows
    (night N was still running at the previous window's start, so it was
    skipped; night N+1 finishes before the next window runs). A newest-only
    selection would report N+1's PASS and never surface N's FAILURE. The
    newest FAILURE within the floor must win over a newer PASS, or a real
    failure silently ages out of the ledger the moment anything later
    passes."""
    e2e_line, _ = _run_with_stub_gh(
        tmp_path,
        json_payload=[
            {
                "conclusion": "failure",
                "createdAt": "2026-09-08T04:17:00Z",
                "url": "https://example/runs/1",
            },
            {
                "conclusion": "success",
                "createdAt": "2026-09-09T04:17:00Z",
                "url": "https://example/runs/2",
            },
        ],
    )
    assert e2e_line == "failure (2026-09-08T04:17:00Z) https://example/runs/1"


def test_multiple_passes_report_the_newest_one(tmp_path: Path) -> None:
    """OPPOSITE DIRECTION: with no failure anywhere in the floor, the row
    must still report the MOST RECENT completed run, not the oldest --
    preferring a failure over a later pass must not accidentally flip the
    ordering when every run passed."""
    e2e_line, _ = _run_with_stub_gh(
        tmp_path,
        json_payload=[
            {
                "conclusion": "success",
                "createdAt": "2026-09-08T04:17:00Z",
                "url": "https://example/runs/1",
            },
            {
                "conclusion": "success",
                "createdAt": "2026-09-09T04:17:00Z",
                "url": "https://example/runs/2",
            },
        ],
    )
    assert e2e_line == "success (2026-09-09T04:17:00Z) https://example/runs/2"


def test_no_completed_runs_is_reported_as_such_not_as_a_pass(tmp_path: Path) -> None:
    """OPPOSITE DIRECTION: zero completed runs within the floor (every
    scheduled run in the window is still in flight, or none exist) must
    render as an explicit "not measured yet" string, never as an empty
    e2e_line that a careless reader could mistake for a blank pass."""
    e2e_line, _ = _run_with_stub_gh(tmp_path, json_payload=[])
    assert "NO COMPLETED SCHEDULED RUNS FOUND" in e2e_line


def test_a_failed_query_is_reported_as_a_failed_measurement(tmp_path: Path) -> None:
    """OPPOSITE DIRECTION again: `gh` itself failing (rate limit, auth) must
    not silently read as "no runs" -- it is a FAILED measurement, not a
    finding."""
    e2e_line, _ = _run_with_stub_gh(tmp_path, fail="boom")
    assert "QUERY FAILED" in e2e_line
