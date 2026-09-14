#!/usr/bin/env python3
"""R2-first stems job worker: hydrate indexed bundles, compute the rest (#2630).

At runtime this worker refreshes the stem index once, hydrates every indexed
track through the production hydration path, then falls through to the
existing Modal/local compute worker only for tracks that are genuinely absent
from the index.

Run directly:
  python scripts/stems_r2_first_worker.py --stable-id <id> [--data-dir DIR]
  python scripts/stems_r2_first_worker.py --scope pending --tier M [--data-dir DIR]
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from apps.cloud import stem_index
from apps.cloud.stem_source import StemSourceError, resolve_stem_hydration_source
from apps.stems.hydrate_runner import HydrateRunnerError, hydrate_stable_ids
from apps.stems.job import (
    LOCAL_WORKER_SCRIPT,
    SCOPE_PENDING,
    UV_BIN,
    WORKER_SCRIPT,
    resolve_transport,
)


def _resolve_pending_ids(data_dir: Path) -> list[str]:
    from apps.stems.selection import library_buckets

    buckets = library_buckets(data_dir)
    print(
        f"[stems-r2-first] scope={SCOPE_PENDING}: {len(buckets.pending)} pending, "
        f"{len(buckets.ready)} already done, "
        f"{len(buckets.unavailable)} with no audio on disk, "
        f"of {buckets.total} library rows",
        file=sys.stderr,
        flush=True,
    )
    return buckets.pending_ids()


def _partition_indexed(
    stable_ids: list[str], data_dir: Path
) -> tuple[list[str], list[str]]:
    source = resolve_stem_hydration_source(data_dir)
    if source is None:
        return [], stable_ids
    source.refresh_index(data_dir, force=True)
    index = stem_index.load_cached_index(data_dir)
    indexed = [sid for sid in stable_ids if sid in index]
    unindexed = [sid for sid in stable_ids if sid not in index]
    return indexed, unindexed


def _compute_argv(
    stable_ids: list[str],
    *,
    data_dir: Path,
    tier: str,
    executor: str,
) -> list[str]:
    if shutil.which(UV_BIN) is None:
        raise SystemExit(
            f"error: {UV_BIN!r} is not on PATH, and the stems compute worker needs uv"
        )
    if executor == "local":
        from apps.stems.local_gate import local_stems_gate

        refusal = local_stems_gate()
        if refusal is not None:
            raise SystemExit(f"error: {refusal}")
        argv: list[str] = [
            UV_BIN,
            "run",
            "--no-sync",
            "python",
            LOCAL_WORKER_SCRIPT,
        ]
    else:
        argv = [
            UV_BIN,
            "run",
            "--no-sync",
            "--with",
            "modal",
            "python",
            WORKER_SCRIPT,
            "--tier",
            tier,
            "--transport",
            resolve_transport(),
        ]
    argv += ["--data-dir", str(data_dir)]
    for stable_id in stable_ids:
        argv += ["--stable-id", stable_id]
    return argv


def _run_compute(
    stable_ids: list[str],
    *,
    data_dir: Path,
    tier: str,
    executor: str,
) -> int:
    argv = _compute_argv(stable_ids, data_dir=data_dir, tier=tier, executor=executor)
    completed = subprocess.run(argv, check=False)
    return int(completed.returncode)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--stable-id", action="append", dest="stable_ids", default=[])
    parser.add_argument("--scope", choices=(SCOPE_PENDING,), default=None)
    parser.add_argument("--tier", default="M")
    parser.add_argument("--executor", choices=("modal", "local"), default="modal")
    args = parser.parse_args(argv)

    if (args.stable_ids and args.scope) or (not args.stable_ids and not args.scope):
        raise SystemExit("error: pass exactly one of --stable-id (repeatable) or --scope")

    data_dir = args.data_dir
    if data_dir is None:
        from apps.shared.platform_paths import DATA_DIR

        data_dir = DATA_DIR
    data_dir = Path(data_dir)

    if args.scope == SCOPE_PENDING:
        stable_ids = _resolve_pending_ids(data_dir)
    else:
        stable_ids = list(dict.fromkeys(args.stable_ids))

    if not stable_ids:
        print("[stems-r2-first] nothing to do", file=sys.stderr, flush=True)
        return 0

    source = resolve_stem_hydration_source(data_dir)
    if source is None:
        return _run_compute(
            stable_ids,
            data_dir=data_dir,
            tier=args.tier,
            executor=args.executor,
        )

    try:
        indexed, unindexed = _partition_indexed(stable_ids, data_dir)
    except StemSourceError as exc:
        raise SystemExit(f"error: {exc.code}: {exc.message}") from exc

    if indexed:
        try:
            hydrate_stable_ids(indexed, data_dir, refresh_index=False)
        except HydrateRunnerError as exc:
            raise SystemExit(f"error: {exc}") from exc

    if not unindexed:
        return 0
    return _run_compute(
        unindexed,
        data_dir=data_dir,
        tier=args.tier,
        executor=args.executor,
    )


if __name__ == "__main__":
    raise SystemExit(main())
