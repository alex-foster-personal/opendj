"""`scripts/*.py` entrypoints only resolve their own `scripts` package import
when run as `-m scripts.<name>`, never as a plain `python scripts/<name>.py`
(#T8, found gating the batch-9e pin PRs Sat 5 Sep 2026). A bare
`ModuleNotFoundError: No module named 'scripts'` from the latter form is not
actionable, so the affected entrypoints now catch it and name the working
invocation instead.

`scripts/review_coverage.py` carries the guard too (added in the P1 fix
round, Sun 6 Sep 2026): its own `_body_is_at_head` helper (plus the two regex
constants it used) moved to scripts/review_gh.py first, following this
module's own documented precedent for shedding lines under file-size
pressure, which is what made room for the guard under the 600-line ratchet
without touching ops/quality/baseline.json. It is checked here only for the
direct-vs-module invocation split, not the `--help` smoke test below: its
`-m` form has a pre-existing, unrelated `JSONDecodeError` on `--help`
(reproduced against origin/main before this task touched the file), so it is
excluded from that parametrize rather than asserted against a bug this task
did not introduce and is not scoped to fix.

[if] `uv run --no-sync python -m scripts.quality_gate --help` is run [then] it
    exits 0, same as before the guard was added
[if] `python scripts/quality_gate.py --help` is run directly [then] it exits
    non-zero with a message naming `-m scripts.quality_gate`, not a bare
    ModuleNotFoundError traceback
[if] `pin_mark_merged`'s parser sees `--dry-run` with no leading `--` [then]
    it parses (this is the form `just pin-merged N --dry-run` now forwards,
    replacing the broken `just pin-merged N -- --dry-run` the justfile used
    to document)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.feedback_harvest import main as feedback_harvest_main
from scripts.pin_mark_merged import main as pin_mark_merged_main

REPO_ROOT = Path(__file__).resolve().parents[2]

# Every scripts/*.py entrypoint carrying the module-only `from scripts import
# ...` guard (T8). Kept as an explicit list, not a glob over "has __main__",
# so a future entrypoint added without the guard fails this test loudly
# instead of silently going unchecked.
GUARDED_ENTRYPOINTS = [
    "scripts/backup_music_to_bifrost2.py",
    "scripts/build_engine_payload.py",
    "scripts/ci_cost_ledger.py",
    "scripts/ci_eval_suite.py",
    "scripts/ci_health_check.py",
    "scripts/feedback_prompts_export.py",
    "scripts/iteration_metrics_report.py",
    "scripts/pin_mark_merged.py",
    "scripts/pr_ci_coverage.py",
    "scripts/provenance_cli.py",
    "scripts/quality_gate.py",
    "scripts/quality_rubric.py",
    "scripts/redteam_filing.py",
    "scripts/redteam_local_api.py",
    "scripts/redteam_trigger.py",
    "scripts/review_coverage.py",
    "scripts/review_thread_triage.py",
    "scripts/sol_review.py",
    "scripts/trunk_job_verdict.py",
]


def _module_name(rel_path: str) -> str:
    return rel_path[: -len(".py")].replace("/", ".")


@pytest.mark.parametrize("rel_path", ["scripts/pin_mark_merged.py", "scripts/quality_gate.py", "scripts/quality_rubric.py"])
def test_module_form_help_exits_zero(rel_path: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", _module_name(rel_path), "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("rel_path", GUARDED_ENTRYPOINTS)
def test_direct_form_names_the_module_invocation_instead_of_bare_modulenotfounderror(
    rel_path: str,
) -> None:
    result = subprocess.run(
        [sys.executable, rel_path, "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert "Traceback" not in result.stderr, (
        f"{rel_path} raised a bare traceback instead of the actionable guard message"
    )
    assert f"-m {_module_name(rel_path)}" in result.stderr


def test_pin_mark_merged_dry_run_without_leading_dashdash_parses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`just pin-merged N --dry-run` (no `--`) is the form the justfile now
    forwards after stripping any leading `-- `; confirm argparse accepts it."""
    body = tmp_path / "body.md"
    body.write_text("pin b44c957f082f -> b63f6d7b\n", encoding="utf-8")
    monkeypatch.setattr("scripts.pin_mark_merged._discover", lambda *a, **k: [])
    monkeypatch.setattr("scripts.pin_mark_merged._gh_repo", lambda: "owner/repo")
    monkeypatch.setattr("scripts.pin_mark_merged._env_ports", list)
    # No configured --url and no discovered daemons: the run correctly gets as
    # far as "no reachable feedback daemon" (exit 1) instead of argparse
    # rejecting --dry-run as an unrecognized positional (exit 2), which is
    # what `just pin-merged N -- --dry-run` did before this fix.
    result = pin_mark_merged_main(["914", "--body", str(body), "--dry-run", "--skip-silver"])
    assert result == 1


def test_feedback_harvest_dry_run_without_leading_dashdash_parses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`just feedback-harvest --dry-run` (no `--`) is the P2 sibling fix: the
    same literal `--` forwarding bug, three lines above `pin-merged` in the
    justfile, now stripped the same way. Confirm argparse accepts the flag
    with no reachable daemon, rather than rejecting it as an unrecognized
    positional the way `just feedback-harvest -- --dry-run` did before."""
    monkeypatch.setattr("scripts.feedback_harvest._discover", lambda *a, **k: [])
    result = feedback_harvest_main(["--dry-run", "--skip-silver"])
    assert result == 1


def _just_dry_run(recipe_args: list[str]) -> str:
    """The fully-expanded shell command `just` would run for this recipe
    invocation, via `just -n` -- its own dry-run/preview mode, unrelated to
    any script's own `--dry-run` flag. Nothing is executed: `-n` only prints,
    so this is fast, hermetic, and (critically for `pin-merged`) never
    actually invokes `scripts.pin_mark_merged`, real PR number or not.
    """
    result = subprocess.run(
        ["just", "-n", *recipe_args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    # `just -n` prints the expanded command to STDERR, not stdout -- verified
    # directly (`just -n feedback-harvest --dry-run 2>/dev/null` prints
    # nothing; the command shows up only with stderr captured).
    return result.stderr.strip()


@pytest.mark.parametrize(
    "recipe_args",
    [
        ["feedback-harvest", "--dry-run", "--skip-silver"],
        ["feedback-harvest", "--", "--dry-run", "--skip-silver"],
    ],
    ids=["no-leading-dashdash", "leading-dashdash"],
)
def test_feedback_harvest_recipe_forwards_dry_run_regardless_of_leading_dashdash(
    recipe_args: list[str],
) -> None:
    """Exercises the justfile RECIPE, not the Python entrypoint directly.

    sol-review v1 P1 BLOCKING (PR #1363, Sun 6 Sep 2026): the prior version
    of this test called `feedback_harvest_main` directly with an
    already-correct argv, so it never exercised `trim_start_match` -- the one
    thing the justfile fix actually changed. Reverting `trim_start_match`
    left that test green (see the PR body for the revert-and-watch-it-fail
    proof this finding demanded). Asserting on `just -n`'s printed command
    instead means a regression in the recipe's own forwarding is what fails
    this test, not a regression in argparse three layers away from it.
    """
    assert _just_dry_run(recipe_args) == (
        "uv run --no-sync python -m scripts.feedback_harvest --dry-run --skip-silver"
    )


@pytest.mark.parametrize(
    "recipe_args",
    [
        ["pin-merged", "999999", "--dry-run"],
        ["pin-merged", "999999", "--", "--dry-run"],
    ],
    ids=["no-leading-dashdash", "leading-dashdash"],
)
def test_pin_merged_recipe_forwards_dry_run_regardless_of_leading_dashdash(
    recipe_args: list[str],
) -> None:
    """Same recipe-layer gap as feedback-harvest above, same
    `trim_start_match` guard in the same justfile. `just -n` only prints the
    command it would run -- it is never executed, so 999999 never has to be
    a real PR and `scripts.pin_mark_merged` never actually runs.
    """
    assert _just_dry_run(recipe_args) == (
        "uv run --no-sync python -m scripts.pin_mark_merged 999999 --dry-run"
    )
