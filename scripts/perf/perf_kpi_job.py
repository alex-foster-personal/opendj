"""Perf KPI job unit: nightly deck-load capture and live engine health probe.

Requirements (issue #1506):
- [if] warm anlz median exceeds 3x the trailing 7-day ledger median [then] exit non-zero
  and record a ceiling breach [else broken].
- [if] two consecutive live health probes time out [then] restart the preview engine once
  [else broken].
- [if] a probe cannot measure [then] append an error ledger row, never a number [else broken].

-Codex
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from scripts.perf.perf_kpi_config import (
    REPO_ROOT,
    load_config,
    require_machine_label,
)
from scripts.perf.perf_kpi_health import HealthConfig, build_restart_command, run_health_tick
from scripts.perf.perf_kpi_ledger_local import refuse_ledger_edits_made_during_run
from scripts.perf.perf_kpi_ledger_pr import update_ledger_pr
from scripts.perf.perf_kpi_nightly import (
    acquire_nightly_engine,
    default_probe,
    run_nightly,
    stop_scratch_engine,
)


def _git_sha(repo_root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    return completed.stdout.strip()


def _engine_log_path() -> Path:
    return (
        Path.home() / "Library/Application Support/com.opendj.desktop.chrome-loop/logs/engine.log"
    )


def cmd_health(config) -> int:
    restart = build_restart_command(config.preview_engine_label)
    return run_health_tick(
        HealthConfig(
            health_url=config.preview_health_url,
            state_file=config.health_state,
            health_log=config.health_log,
            timeout_s=5.0,
            failure_threshold=2,
            restart_command=restart,
            engine_log_path=_engine_log_path(),
        )
    )


def cmd_nightly(config, *, base_url: str | None, skip_pr: bool) -> int:
    require_machine_label(config)
    url, proc, _log_path, prep_code = acquire_nightly_engine(config, base_url=base_url)
    if prep_code != 0:
        return prep_code
    # Captured BEFORE run_nightly ever touches the tracked ledger (Sol, PR
    # #3827, P1/BLOCKING, review 4107678114): this is the one point in the
    # whole flow where REPO_ROOT's own file still holds only whatever the
    # caller had before tonight's job started, dirty or clean. See
    # update_ledger_pr's docstring for why it cannot be captured any later.
    pre_run_content = (
        config.ledger_path.read_text(encoding="utf-8") if config.ledger_path.exists() else None
    )
    try:
        outcome = run_nightly(
            config,
            base_url=url,
            git_sha=_git_sha(REPO_ROOT),
            probe=default_probe,
            file_issue=not skip_pr,
        )
        if not skip_pr:
            validated_content = refuse_ledger_edits_made_during_run(
                config.ledger_path,
                pre_run_content=pre_run_content,
                append_batches=outcome.append_batches,
            )
            update_ledger_pr(
                REPO_ROOT,
                config.ledger_path,
                config.ledger_worktree,
                pre_run_content=pre_run_content,
                post_run_content=validated_content,
            )
        return outcome.exit_code
    finally:
        if proc is not None:
            stop_scratch_engine(proc)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("nightly", "health"))
    parser.add_argument("--base-url", help="throwaway engine base URL for nightly mode")
    parser.add_argument("--skip-pr", action="store_true", help="do not open/update ledger PR")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config()
    if args.mode == "health":
        return cmd_health(config)
    return cmd_nightly(config, base_url=args.base_url, skip_pr=args.skip_pr)


if __name__ == "__main__":
    raise SystemExit(main())
