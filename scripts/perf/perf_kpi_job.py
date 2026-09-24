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
import shutil
import subprocess
from pathlib import Path

from scripts.perf.perf_kpi_config import (
    LEDGER_PR_BRANCH,
    LEDGER_PR_TITLE,
    LEDGER_WORKTREE_DIR,
    REPO_ROOT,
    load_config,
)
from scripts.perf.perf_kpi_health import HealthConfig, build_restart_command, run_health_tick
from scripts.perf.perf_kpi_nightly import (
    acquire_nightly_engine,
    default_probe,
    run_nightly,
    stop_scratch_engine,
)

REPOSITORY = "maintainer/music-dj-tools"


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
    url, proc, _log_path, prep_code = acquire_nightly_engine(config, base_url=base_url)
    if prep_code != 0:
        return prep_code
    try:
        outcome = run_nightly(
            config,
            base_url=url,
            git_sha=_git_sha(REPO_ROOT),
            probe=default_probe,
            file_issue=not skip_pr,
        )
        if not skip_pr:
            update_ledger_pr(REPO_ROOT, config.ledger_path)
        return outcome.exit_code
    finally:
        if proc is not None:
            stop_scratch_engine(proc)


def _remove_worktree_if_present(repo_root: Path, worktree_dir: Path) -> None:
    """Best-effort cleanup of a leftover ledger worktree from a prior run."""
    if not worktree_dir.exists():
        return
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree_dir)],
        check=False,
        cwd=repo_root,
        capture_output=True,
    )
    if worktree_dir.exists():
        shutil.rmtree(worktree_dir, ignore_errors=True)


def update_ledger_pr(
    repo_root: Path,
    ledger_path: Path,
    worktree_dir: Path = LEDGER_WORKTREE_DIR,
) -> None:
    """Open or update the standing docs PR for nightly ledger appends.

    Runs entirely inside a DEDICATED worktree, never ``repo_root`` itself
    (Codex review, PR #3827): the nightly launchd job installed on silver
    shares ``repo_root`` with whatever checkout lives there, and the old
    ``git checkout -B <branch> origin/main`` run directly in it would switch
    that checkout's branch out from under any in-progress work the moment
    the job fires at 04:00.
    """
    completed = subprocess.run(
        ["gh", "pr", "list", "--repo", REPOSITORY, "--head", LEDGER_PR_BRANCH, "--json", "number"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0 and completed.stdout.strip() not in ("", "[]"):
        return
    branch = LEDGER_PR_BRANCH
    subprocess.run(["git", "fetch", "origin", "main"], check=True, cwd=repo_root)
    _remove_worktree_if_present(repo_root, worktree_dir)
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-B", branch, str(worktree_dir), "origin/main"],
        check=True,
        cwd=repo_root,
    )
    try:
        dest = worktree_dir / "docs" / "perf" / "kpi-ledger.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(ledger_path.read_text(encoding="utf-8"), encoding="utf-8")
        subprocess.run(["git", "add", "docs/perf/kpi-ledger.json"], check=True, cwd=worktree_dir)
        subprocess.run(
            ["git", "commit", "-m", "perf(kpi): nightly ledger append\n\n-Codex"],
            check=True,
            cwd=worktree_dir,
        )
        subprocess.run(["git", "push", "-u", "origin", branch], check=True, cwd=worktree_dir)
    finally:
        _remove_worktree_if_present(repo_root, worktree_dir)
    subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            REPOSITORY,
            "--base",
            "main",
            "--head",
            branch,
            "--title",
            LEDGER_PR_TITLE,
            "--body",
            "Standing docs PR for nightly perf KPI ledger appends. Never merges by itself.",
        ],
        check=True,
        cwd=repo_root,
    )


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
