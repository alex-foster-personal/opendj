"""``from-stems`` subcommand (split from ``apps.vocals.cli``).

Requirements (mini-PRD):
  ✔︎ ✅ from-stems: CPU-only backfill of vocal-cache from existing
    ``(private path)/<id>/`` bundles (no demucs). Requires explicit
    --dry-run or --live (no silent default). Skips valid cache unless --force.
    [if] neither --dry-run nor --live [then ⛔️] argparse rejects
    [if] stems present and cache missing [then] --live writes regions
    [if] cache valid and no --force [then] skip

-Claude
"""

from __future__ import annotations

import argparse
import sys
import time

from apps.vocals import cache as vcache
from apps.vocals import from_stems as vfrom_stems
from apps.vocals.cli_library import (
    Ctx,
    VocalTrack,
    load_state_tracks,
)


def cmd_from_stems(args: argparse.Namespace) -> int:  # noqa: C901
    """CPU backfill: stem bundles -> vocal-cache (no demucs)."""
    from apps.stems.artifacts import (
        StemArtifactError,
        StemBundleNotFoundError,
        load_stem_bundle,
    )

    ctx = Ctx(data_dir=args.data_dir)
    root = vfrom_stems.stems_dir(ctx.data_dir)
    live = bool(args.live)
    force = bool(args.force)

    if args.stable_id:
        try:
            load_stem_bundle(args.stable_id, stems_dir=root)
        except StemBundleNotFoundError:
            raise SystemExit(  # noqa: B904
                f"error: no stem bundle for {args.stable_id!r} under {root}"
            )
        except StemArtifactError as exc:
            raise SystemExit(  # noqa: B904
                f"error: stem bundle invalid for {args.stable_id!r}: {exc}"
            )
        ids = [args.stable_id]
    else:
        ids = vfrom_stems.list_bundle_ids(root)

    by_id = {t.stable_id: t for t in load_state_tracks(ctx)}
    planned: list[VocalTrack] = []
    for sid in ids:
        tr = by_id.get(sid)
        if tr is None:
            print(
                f"[SKIP unknown-track] {sid}: no live track in {ctx.state_db}",
                file=sys.stderr,
            )
            continue
        if tr.audio_path is None or not tr.audio_path.is_file():
            print(
                f"[SKIP missing-file] {sid} {tr.title!r}: no local audio file",
                file=sys.stderr,
            )
            continue
        cache_file = vcache.cache_path(ctx.data_dir, sid)
        existing = vcache.load_valid_entry(cache_file, tr.audio_path)
        if existing is not None and not force:
            print(
                f"[SKIP cached] {sid} {tr.title!r}: {len(existing['regions'])} regions cov={existing['coverage_pct']}%"
            )
            continue
        planned.append(tr)

    if args.limit is not None:
        planned = planned[: max(0, int(args.limit))]

    mode = "live" if live else "dry-run"
    print(f"[{mode}] from-stems: {len(planned)} track(s) (bundles under {root}; force={force})")
    for i, tr in enumerate(planned, 1):
        print(f"  {i}. {tr.stable_id} {tr.title!r}")
    if not live:
        print("[dry-run] no writes; pass --live to publish vocal-cache entries")
        return 0

    written = 0
    for i, tr in enumerate(planned, 1):
        assert tr.audio_path is not None
        t0 = time.perf_counter()
        try:
            entry = vfrom_stems.write_from_bundle(
                ctx.data_dir,
                tr.stable_id,
                tr.audio_path,
                stems_root=root,
            )
        except Exception as exc:  # noqa: BLE001
            print(
                f"[FAIL] {tr.stable_id} {tr.title!r}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            continue
        wall_s = time.perf_counter() - t0
        written += 1
        print(
            f"[{i}/{len(planned)}] {tr.stable_id} {tr.title!r} "
            f"regions={len(entry['regions'])} cov={entry['coverage_pct']}% "
            f"in {wall_s:.2f}s -> {vcache.cache_path(ctx.data_dir, tr.stable_id)}"
        )
    print(f"[live] finished: {written} written, {len(planned) - written} failed")
    return 0 if written == len(planned) else 1
