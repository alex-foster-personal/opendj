"""Command line for the STEM-58 re-compress pass (:mod:`apps.stems.recompress`).

Plans the pass, prints it, and on ``--live`` swaps each eligible bundle and
reports the result. Every write goes through :mod:`apps.stems.recompress`;
this module only parses arguments, prints and writes the JSON report.
``python -m apps.stems.recompress`` still runs it.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from apps.stems.recompress import (
    BACKUP_DIR_NAME,
    ELIGIBLE,
    Candidate,
    Outcome,
    StemsJobLiveError,
    assert_no_live_stems_job,
    default_backup_root,
    orphaned_swaps,
    plan,
    recompress_bundle,
    recover_orphaned_swaps,
)
from apps.stems.stem_encode import require_mp3_encoder


def _mb(n: int) -> str:
    return f"{n / 1e6:,.1f} MB"


def _print_plan(candidates: Sequence[Candidate]) -> None:
    counts = Counter(c.verdict for c in candidates)
    sizes: Counter[str] = Counter()
    for c in candidates:
        sizes[c.verdict] += c.bundle_bytes
    print(f"{len(candidates)} bundle(s) under the stem roots")
    for verdict, n in counts.most_common():
        print(f"  {verdict:<28} {n:>6}  {_mb(sizes[verdict]):>12}")


def _bundle_count(text: str) -> int:
    """``--limit``: a count of bundles, refused at parse time when negative.

    ``eligible[:-1]`` is every bundle but the last, so ``--live --limit -1``
    rewrote nearly the whole library (Sol on #6820).
    """
    count = int(text)
    if count < 0:
        raise argparse.ArgumentTypeError(f"--limit is a number of bundles, not {count}")
    return count


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m apps.stems.recompress",
        description="Re-compress lossless stem bundles of MP3/M4A sources to MP3 (dry run by default).",
    )
    parser.add_argument("--data-dir", type=Path, default=None, help="default: the app's data dir")
    parser.add_argument("--live", action="store_true", help="write; without it nothing changes")
    parser.add_argument("--limit", type=_bundle_count, default=None, help="at most N bundles this run")
    parser.add_argument("--stable-id", action="append", default=[], help="only these ids")
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=None,
        help=(
            "same volume as the stem roots (the swap is a rename); "
            "default: stems-recompress-backup/<stamp> beside each resolved stem root"
        ),
    )
    parser.add_argument("--no-jobs-db-ok", action="store_true")
    parser.add_argument("--json", type=Path, default=None, help="write the plan and outcomes here")
    return parser


@dataclasses.dataclass
class PassResult:
    """What one live pass did: swapped bundles, counted failures, and why it stopped early."""

    outcomes: list[Outcome] = dataclasses.field(default_factory=list)
    failures: list[str] = dataclasses.field(default_factory=list)
    stopped: str | None = None


def _backup_roots_used(result: PassResult, backup_root: Path | None, stamp: str) -> str:
    if backup_root is not None:
        return str(backup_root)
    roots = sorted({str(o.backup_dir.parent.parent) for o in result.outcomes})
    return ", ".join(roots) if roots else f"{BACKUP_DIR_NAME}/{stamp} beside each stem root"


def _live_pass(
    data_dir: Path,
    eligible: Sequence[Candidate],
    backup_root: Path | None,
    *,
    stamp: str,
    no_jobs_db_ok: bool,
) -> PassResult:
    """Re-compress every eligible bundle, re-checking for a live stems job before each swap.

    With no ``backup_root`` each bundle's backup goes to
    :func:`default_backup_root` for its own stem root.
    """

    def refuse_if_a_stems_job_went_live() -> None:
        assert_no_live_stems_job(data_dir, missing_ok=no_jobs_db_ok)

    result = PassResult()
    for index, candidate in enumerate(eligible, 1):
        try:
            outcome = recompress_bundle(
                candidate,
                backup_root if backup_root is not None else default_backup_root(candidate.root, stamp),
                before_swap=refuse_if_a_stems_job_went_live,
            )
        except StemsJobLiveError as exc:
            result.stopped = (
                f"stopped before swapping {candidate.stable_id} ({index} of {len(eligible)}): {exc}; "
                f"{len(eligible) - index + 1} bundle(s) left as they were"
            )
            print(f"refused: {result.stopped}", file=sys.stderr)
            break
        except Exception as exc:  # noqa: BLE001 - one bad bundle never stops the pass; it is counted
            result.failures.append(f"{candidate.stable_id}: {exc}")
            print(f"[{index}/{len(eligible)}] FAILED {candidate.stable_id}: {exc}", file=sys.stderr)
            continue
        result.outcomes.append(outcome)
        print(
            f"[{index}/{len(eligible)}] {outcome.stable_id}: {_mb(outcome.before_bytes)} -> "
            f"{_mb(outcome.after_bytes)} (source {_mb(outcome.source_bytes)}, "
            f"{len(outcome.violations)} part(s) over source)"
        )
    before = sum(o.before_bytes for o in result.outcomes)
    after = sum(o.after_bytes for o in result.outcomes)
    print(
        f"re-compressed {len(result.outcomes)} bundle(s): {_mb(before)} -> {_mb(after)}; "
        f"{len(result.failures)} failed; old bundles kept in {_backup_roots_used(result, backup_root, stamp)}"
    )
    return result


def _write_report(path: Path, candidates: Sequence[Candidate], result: PassResult) -> None:
    path.write_text(
        json.dumps(
            {
                "plan": [
                    {**dataclasses.asdict(c), "root": str(c.root), "source_path": str(c.source_path or "")}
                    for c in candidates
                ],
                "outcomes": [{**dataclasses.asdict(o), "backup_dir": str(o.backup_dir)} for o in result.outcomes],
                "failures": result.failures,
                "stopped": result.stopped,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _prepare_pass(data_dir: Path, *, live: bool, no_jobs_db_ok: bool) -> bool:
    """Repair interrupted swaps before a live pass, or list them on a dry run; False refuses the pass."""
    if live:
        # Nothing is written, recovery included, while a stems job is live or
        # cannot be ruled out (Codex on #6820); _live_pass re-checks per swap.
        try:
            assert_no_live_stems_job(data_dir, missing_ok=no_jobs_db_ok)
        except StemsJobLiveError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return False
        # A pass killed between its two renames left a track with only its
        # backup; put it back before planning, or plan() never sees the bundle.
        # Recovery is renames only, so it runs before the encoder probe: a host
        # that has lost its MP3 writer still gets its stranded bundles back
        # (Codex on #6820).
        for line in recover_orphaned_swaps(data_dir):
            print(f"recovered: {line}")
        require_mp3_encoder()
    else:
        for journal in orphaned_swaps(data_dir):
            print(f"dry run: --live would first repair the interrupted swap {journal}")
    return True


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.data_dir is None:
        from apps.shared.platform_paths import DATA_DIR

        args.data_dir = DATA_DIR
    data_dir = Path(args.data_dir)
    if not _prepare_pass(data_dir, live=args.live, no_jobs_db_ok=args.no_jobs_db_ok):
        return 3
    candidates = plan(data_dir, only=args.stable_id)
    _print_plan(candidates)
    eligible = [c for c in candidates if c.verdict == ELIGIBLE]
    if args.limit is not None:
        eligible = eligible[: args.limit]
    result = PassResult()
    if not args.live:
        print(
            f"dry run: {len(eligible)} bundle(s) would be re-compressed ({_mb(sum(c.bundle_bytes for c in eligible))}); pass --live"
        )
    else:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        result = _live_pass(data_dir, eligible, args.backup_dir, stamp=stamp, no_jobs_db_ok=args.no_jobs_db_ok)
    if args.json is not None:
        _write_report(args.json, candidates, result)
    if result.stopped is not None:
        return 3
    return 1 if result.failures else 0
