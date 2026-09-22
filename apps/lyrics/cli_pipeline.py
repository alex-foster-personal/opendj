"""Pipeline subcommands of ``python -m apps.lyrics`` (PR-3, D13.3/D13.4).

Parser wiring and dispatch for ``batch``, ``jobs``, ``register-stems`` and
``stems``, in their own module so ``apps/lyrics/__main__.py`` stays under the
600-line gate.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from apps.lyrics import register_stems, stems_sync, worker
from apps.lyrics.batch import BatchPaths, StageBlocked, batch_paths_for, run_batch
from apps.shared.paths import DATA_DIR, PROJECT_ROOT, STATE_DIR

PIPELINE_COMMANDS: frozenset[str] = frozenset(
    {"batch", "jobs", "register-stems", "stems"}
)


def add_pipeline_commands(subcommands: argparse._SubParsersAction) -> None:
    """Register batch/jobs/register-stems/stems on ``subcommands``."""
    batch = subcommands.add_parser("batch", help="lyric batch driver (section 12)")
    batch_sub = batch.add_subparsers(dest="batch_command", required=True)
    for name, help_text in (
        ("run", "dry-run by default; pass --live to execute"),
        ("resume", "always live; registered bundles symlink in"),
    ):
        p = batch_sub.add_parser(name, help=help_text)
        p.add_argument("--tracks", type=Path, required=True, help="JSON list of stable_ids")
        p.add_argument("--corpus", required=True, help="corpus name under lyrics-eval")
        p.add_argument(
            "--state-dir",
            type=Path,
            default=STATE_DIR,
            help="directory holding state.db (default: repo data/state)",
        )
        if name == "run":
            p.add_argument("--live", action="store_true", help="execute stages")

    jobs = subcommands.add_parser("jobs", help="lyric job queue consumer")
    jobs_sub = jobs.add_subparsers(dest="jobs_command", required=True)
    work = jobs_sub.add_parser("work", help="consume queued jobs")
    work.add_argument("--once", action="store_true", help="process one job then exit")
    work.add_argument(
        "--state-dir",
        type=Path,
        default=STATE_DIR,
        help="directory holding state.db (default: repo data/state)",
    )

    reg = subcommands.add_parser(
        "register-stems",
        help="register lyric-pipeline stems as canonical bundles",
    )
    reg.add_argument(
        "--corpus",
        required=True,
        choices=["crate", "own-crate", "oltf", "batch1", "batch2", "all"],
    )
    reg.add_argument("--write", action="store_true", help="write bundles; omit for dry-run")

    stems = subcommands.add_parser("stems", help="CloudSync stem bundle push/hydrate")
    stems_sub = stems.add_subparsers(dest="stems_command", required=True)
    push = stems_sub.add_parser("push", help="push local bundles to R2")
    push.add_argument(
        "--missing",
        action="store_true",
        help="upload bundles absent from R2 (by matching object size)",
    )
    push.add_argument("--dry-run", action="store_true")
    push.add_argument("--data-dir", type=Path, default=DATA_DIR)

    hydrate = stems_sub.add_parser("hydrate", help="fetch a bundle by manifest hashes")
    hydrate.add_argument("stable_id")
    hydrate.add_argument("--manifest", type=Path, required=True)
    hydrate.add_argument("--dry-run", action="store_true")
    hydrate.add_argument("--data-dir", type=Path, default=None)


def _batch_paths(state_dir: Path) -> BatchPaths:
    return batch_paths_for(state_dir)


def _load_stable_ids(path: Path) -> list[str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise SystemExit(f"[ERROR] --tracks must be a non-empty JSON list: {path}")
    return [str(x) for x in raw]


def _cmd_batch(args: argparse.Namespace) -> int:
    stable_ids = _load_stable_ids(args.tracks)
    paths = _batch_paths(args.state_dir)
    live = args.batch_command == "resume" or getattr(args, "live", False)
    try:
        report = run_batch(
            corpus=args.corpus,
            stable_ids=stable_ids,
            live=live,
            paths=paths,
            repo_root=PROJECT_ROOT,
        )
    except StageBlocked as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    if not live:
        return 0
    return 0 if report.ingested >= 0 else 1


def _cmd_jobs(args: argparse.Namespace) -> int:
    if args.jobs_command != "work":
        raise AssertionError(f"unhandled jobs subcommand {args.jobs_command!r}")
    if args.once:
        worker.work_once(args.state_dir)
        return 0
    worker.work_loop(args.state_dir)
    return 0


def _cmd_register_stems(args: argparse.Namespace) -> int:
    corpora = register_stems.REGISTRABLE_CORPORA
    targets = list(corpora) if args.corpus == "all" else [args.corpus]
    exit_code = 0
    for corpus in targets:
        sweep = register_stems.sweep_corpus(corpus, write=args.write)
        print(sweep.summary())
        if sweep.failed:
            exit_code = 1
    return exit_code


def _cmd_stems(args: argparse.Namespace) -> int:
    if args.stems_command == "push":
        if not args.missing:
            raise SystemExit(
                "[ERROR] stems push requires --missing "
                "(upload local bundles absent from R2)"
            )
        return stems_sync.push_missing(data_dir=args.data_dir, dry_run=args.dry_run)
    if args.stems_command == "hydrate":
        try:
            return stems_sync.hydrate(
                args.stable_id,
                manifest_path=args.manifest,
                data_dir=args.data_dir,
                dry_run=args.dry_run,
            )
        except (ValueError, Exception) as exc:
            from apps.cloud.eviction import HydrationError

            if isinstance(exc, (HydrationError, ValueError)):
                print(f"[ERROR] {exc}", file=sys.stderr)
                return 1
            raise
    raise AssertionError("unhandled stems subcommand")


def cmd_pipeline(args: argparse.Namespace) -> int:
    """Dispatch pipeline subcommands."""
    if args.command == "batch":
        return _cmd_batch(args)
    if args.command == "jobs":
        return _cmd_jobs(args)
    if args.command == "register-stems":
        return _cmd_register_stems(args)
    if args.command == "stems":
        return _cmd_stems(args)
    raise AssertionError(f"unhandled pipeline command {args.command!r}")


__all__ = ["PIPELINE_COMMANDS", "add_pipeline_commands", "cmd_pipeline"]
