"""`scripts/*.py` entrypoints only resolve their own `scripts` package import
when run as `-m scripts.<name>`, never as a plain `python scripts/<name>.py`
(#T8, found gating the batch-9e pin PRs Sat 5 Sep 2026). A bare
`ModuleNotFoundError: No module named 'scripts'` from the latter form is not
actionable, so the affected entrypoints now catch it and name the working
invocation instead.

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
    "scripts/ci_fixer.py",
    "scripts/ci_health_check.py",
    "scripts/feedback_prompts_export.py",
    "scripts/iteration_metrics_report.py",
    "scripts/pin_mark_merged.py",
    "scripts/pr_ci_coverage.py",
    "scripts/provenance_cli.py",
    "scripts/quality_gate.py",
    "scripts/redteam_filing.py",
    "scripts/redteam_trigger.py",
    "scripts/review_thread_triage.py",
    "scripts/sol_review.py",
    "scripts/trunk_job_verdict.py",
]


def _module_name(rel_path: str) -> str:
    return rel_path[: -len(".py")].replace("/", ".")


@pytest.mark.parametrize("rel_path", ["scripts/pin_mark_merged.py", "scripts/quality_gate.py"])
def test_module_form_help_exits_zero(rel_path: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", _module_name(rel_path), "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
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
