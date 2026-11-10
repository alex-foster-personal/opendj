"""``python -m apps.vocals`` - vocal-detection demucs gap-fill trickle CLI.

Fills the PVDI gap (61% of the library lacks rekordbox 7 vocal analysis,
SPIKE-B1) by trickling tracks through the standalone demucs worker
(``scripts/vocal_region_worker.py``, PEP 723, run via ``uv run`` so
torch/demucs never enter the repo venv) and writing
``(private path)/{stable_id}.json`` per the apps.vocals.cache
contract. The webui /anlz endpoint consults that cache when PVDI is
absent (status "demucs").

Module layout: library model and classification in ``cli_library``, track
claims in ``cli_claims``, worker config and preflight in ``cli_worker``, the
scan and from-stems subcommands in ``cli_scan`` / ``cli_from_stems``, argparse
in ``cli_parser``; this module keeps the demucs run path and re-exports every
moved name so ``apps.vocals.cli.<name>`` keeps working.

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 trickle: process todo queue N tracks (--limit, default 5), ordered
    by (playlist with most on-disk members) desc then length asc; requires
    explicit --dry-run or --live (no silent default);
    idempotent via cache presence; missing files skipped EXPLICITLY with
    a log line; per-track wall time + running ETA on stdout.
    [if] neither --dry-run nor --live [then ⛔️] argparse rejects
    [if] run twice --live [then] second run recomputes nothing
    [if] a queued file vanished before its turn [then] "[SKIP]" line, no crash
    [if] --dry-run and --live both passed [then ⛔️] argparse rejects them
    [if] --limit is negative [then ⛔️] argparse rejects it before queue slicing
    [if] audio mtime changes during a worker run [then ⛔️] no cache write
  ✔︎ ✅ 🎯 one: analyse a single --stable-id immediately (debug path), --force
    recomputes over a valid cache entry.
    [if] cache valid and no --force [then] no worker run, prints cached
    [if] no local ANLZ .DAT exists [then ⛔️] reject before cache or worker
  ✔︎ ✅ --data-dir overrides the repo-default data/ root everywhere
    (worktrees pass the primary checkout's data dir explicitly).
  ✔︎ ✅ Windows portability: FolderPath/.2EX resolution goes through the
    shared apps.shared.platform_paths.resolve_asset_path resolver (never
    a local /PIONEER/-only shim), so an unmapped foreign-absolute path
    (e.g. a Mac path read on Windows) is an explicit missing state, never
    a fabricated Path; the worker subprocess honours MDT_VOCAL_WORKER_PYTHON
    (bench-env interpreter override) and MDT_VOCAL_WORKER_DEVICE.
    [if] a FolderPath is foreign-absolute with no path-map entry [then]
    audio_on_disk stays False (classification unchanged, falls to missing)
    [if] MDT_VOCAL_WORKER_PYTHON is set [then] run_worker invokes it
    directly, never ``uv run``
  ✔︎ ✅ trickle --max-minutes wall budget (skip-and-continue); Supersedes: scripts/fill_vocal_gap.sh.

Exact command lines:
  python -m apps.vocals scan --data-dir /Users/user/code/music-dj-tools/data
  python -m apps.vocals trickle --limit 1 --live \\
      --data-dir /Users/user/code/music-dj-tools/data
  python -m apps.vocals from-stems --dry-run
  python -m apps.vocals from-stems --live

-Claude
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from apps.shared.platform_paths import (
    load_path_map,
)
from apps.shared.process_groups import WorkerCleanupError as _WorkerCleanupError
from apps.shared.process_tree import TreeRoot, spawn_tree, stop_tree
from apps.vocals import attempts as vattempts
from apps.vocals import cache as vcache
from apps.vocals.cli_claims import (
    TrackClaim,
    _assert_track_claim,  # noqa: F401 - re-export
    _claim_track,
    _managed_track_claim,
    _owned_track_claim,
    _read_claim_record,  # noqa: F401 - re-export
    _release_track_claim,  # noqa: F401 - re-export
)
from apps.vocals.cli_from_stems import (
    cmd_from_stems,
)
from apps.vocals.cli_library import (
    CATEGORY_BROKEN_CACHE,  # noqa: F401 - re-export
    CATEGORY_CACHED,  # noqa: F401 - re-export
    CATEGORY_MISSING,
    CATEGORY_MISSING_ANALYSIS,
    CATEGORY_PVDI,  # noqa: F401 - re-export
    CATEGORY_STEM_DERIVABLE,
    CATEGORY_TODO,  # noqa: F401 - re-export
    DEMUCS_REALTIME_FACTOR,
    Ctx,
    VocalTrack,
    _anlz_data_file,
    _open_ro,  # noqa: F401 - re-export
    _resolve_share_path,  # noqa: F401 - re-export
    _trickle_demucs_candidates,
    best_playlist_rank,
    classify,
    load_state_tracks,  # noqa: F401 - re-export
    load_tracks,
    master_content_rows,  # noqa: F401 - re-export
    order_todo,
    pvdi_present,  # noqa: F401 - re-export
    rekordbox_mappings,  # noqa: F401 - re-export
    track_fits_budget,
    tracks_from_rows,  # noqa: F401 - re-export
)
from apps.vocals.cli_parser import (
    DEFAULT_TRICKLE_LIMIT,  # noqa: F401 - re-export
    build_vocals_parser,
)
from apps.vocals.cli_scan import (
    _counts,  # noqa: F401 - re-export
    _fmt_dur,
    cmd_scan,
)
from apps.vocals.cli_worker import (
    FFMPEG_OVERRIDE_ENV,  # noqa: F401 - re-export
    PACKAGED_WORKER_PYTHON_ENV,
    PACKAGED_WORKER_SCRIPT_ENV,
    WORKER_SCRIPT,
    WORKER_TERMINATE_GRACE_S,
    WORKER_TIMEOUT_S,
    WORKER_UNAVAILABLE_MESSAGE,
    WorkerUnavailableError,
    preflight_worker,
)
from apps.vocals.worker_output import (
    WorkerOutputError,
    parse_worker_stdout,
    streams_excerpt,
)

# Raised when the worker tree may still exist, so its claim must remain held.
# Shared with the engine reaper rather than redeclared: both terminate a
# worker group, both must be catchable by the same `except`, and a second
# same-named class would silently escape the other's handler.
WorkerCleanupError = _WorkerCleanupError


# ----- worker invocation --------------------------------------------------------------


def _worker_command(audio_path: Path) -> list[str]:
    device = os.environ.get("MDT_VOCAL_WORKER_DEVICE", "auto")
    worker_script = Path(os.environ.get(PACKAGED_WORKER_SCRIPT_ENV, WORKER_SCRIPT))
    override_python = os.environ.get(PACKAGED_WORKER_PYTHON_ENV)
    if override_python:
        return [
            override_python,
            str(worker_script),
            "--device",
            device,
            str(audio_path),
        ]
    return [
        "uv",
        "run",
        "--no-sync",
        "--script",
        str(worker_script),
        "--device",
        device,
        str(audio_path),
    ]


def _terminate_worker_tree(proc: subprocess.Popen[str], root: TreeRoot) -> None:
    """Stop and reap the complete worker tree before returning, on every OS.

    ``apps.shared.process_tree`` owns the ladder and the proof: it raises
    WorkerCleanupError (its ProcessTreeError subclass) when any member of the
    tree may still be running, which is what keeps this track's claim held.
    Its one budget covers both rungs, so it gets two of ours: the polite
    signal keeps its WORKER_TERMINATE_GRACE_S and so does the forced confirm.
    """
    stop_tree(root, grace_s=2 * WORKER_TERMINATE_GRACE_S)
    try:
        proc.wait(timeout=WORKER_TERMINATE_GRACE_S)
    except subprocess.TimeoutExpired as exc:
        raise WorkerCleanupError(f"vocal worker {proc.pid} was stopped but its handle did not exit") from exc


def run_worker(audio_path: Path, timeout_s: float = WORKER_TIMEOUT_S) -> dict[str, Any]:
    """Run the demucs worker with a deadline and return its one framed result.

    Both streams are captured: stdout is parsed strictly
    (``apps.vocals.worker_output``), stderr is passed on to this process's
    stderr once the worker has ended and its tail goes into any error.
    """
    if timeout_s <= 0:
        raise ValueError(f"worker timeout must be > 0 seconds, got {timeout_s}")
    preflight_worker(audio_path, os.environ)
    try:
        proc, root = spawn_tree(
            _worker_command(audio_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (FileNotFoundError, PermissionError) as exc:
        raise WorkerUnavailableError(WORKER_UNAVAILABLE_MESSAGE) from exc
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        _terminate_worker_tree(proc, root)
        sys.stderr.write(proc.communicate()[1] or "")
        raise RuntimeError(f"vocal_region_worker timed out after {timeout_s}s for {audio_path}") from exc
    sys.stderr.write(stderr or "")  # the worker's log stays in this process's log
    if proc.returncode != 0:
        raise WorkerOutputError(
            f"vocal_region_worker failed (exit {proc.returncode}) for {audio_path}; {streams_excerpt(stdout, stderr)}"
        )
    return parse_worker_stdout(stdout, returncode=proc.returncode, stderr=stderr, audio_path=audio_path)


# ----- subcommands ----------------------------------------------------------------------


def _process_one(
    ctx: Ctx,
    tr: VocalTrack,
    prefix: str,
    timeout_s: float = WORKER_TIMEOUT_S,
    claim: TrackClaim | None = None,
) -> tuple[float, dict[str, Any]]:
    """Run the worker for one track and write its cache entry.

    The recorded ``audio_mtime`` is captured BEFORE the (multi-minute)
    worker run: if the file is replaced mid-analysis the regions belong
    to the old content, so recording the pre-run mtime guarantees the
    entry self-invalidates against the new file instead of poisoning
    the cache. A detected change also fails loudly right here.

    Every worker attempt (trickle, one, HTTP) starts here, so this is where
    the track's previous failed-attempt record is cleared: it must not
    outlive a newer attempt, whichever path made it.
    """
    assert tr.audio_path is not None
    vattempts.clear_failure(ctx.data_dir, tr.stable_id)
    pre_mtime = tr.audio_path.stat().st_mtime
    pre_signature = vcache.audio_signature(tr.audio_path)
    t0 = time.perf_counter()
    result = run_worker(tr.audio_path, timeout_s)
    post_signature = vcache.audio_signature(tr.audio_path)
    if post_signature != pre_signature:
        raise RuntimeError(
            f"audio file changed during analysis: {tr.audio_path} "
            f"(source identity changed); regions were computed "
            f"from the old content, refusing to cache them"
        )
    if claim is None:
        entry = vcache.write_entry(
            vcache.cache_path(ctx.data_dir, tr.stable_id),
            result,
            tr.audio_path,
            audio_mtime=pre_mtime,
            source_signature=pre_signature,
        )
    else:
        with _owned_track_claim(claim):
            entry = vcache.write_entry(
                vcache.cache_path(ctx.data_dir, tr.stable_id),
                result,
                tr.audio_path,
                audio_mtime=pre_mtime,
                source_signature=pre_signature,
            )
    wall_s = time.perf_counter() - t0
    rate = wall_s / tr.length_s if tr.length_s else float("nan")
    print(
        f"{prefix} done in {wall_s:.1f}s ({rate:.2f}x realtime) "
        f"regions={len(entry['regions'])} cov={entry['coverage_pct']}% "
        f"-> {vcache.cache_path(ctx.data_dir, tr.stable_id)}"
    )
    return wall_s, entry


def cmd_trickle(args: argparse.Namespace) -> int:  # noqa: C901, PLR0912, PLR0915
    ctx = Ctx(data_dir=args.data_dir)
    live = bool(args.live)
    tracks = load_tracks(ctx, args.playlist)
    classify(ctx, tracks)

    # Explicit, never silent: every unprocessable track is logged (stderr -
    # stdout carries the plan / progress lines).
    n_missing = 0
    for tr in tracks:
        if tr.category == CATEGORY_MISSING:
            n_missing += 1
            print(
                f"[SKIP missing-file] {tr.stable_id} {tr.title!r}: {tr.folder_path or '(no FolderPath)'}",
                file=sys.stderr,
            )
    if n_missing:
        print(f"[skipped {n_missing} missing-file tracks; see stderr]")

    n_missing_analysis = 0
    for tr in tracks:
        if tr.category == CATEGORY_MISSING_ANALYSIS:
            n_missing_analysis += 1
            print(
                f"[SKIP missing-analysis] {tr.stable_id} {tr.title!r}: "
                f"{tr.analysis_data_path or '(no AnalysisDataPath)'}",
                file=sys.stderr,
            )
    if n_missing_analysis:
        print(f"[skipped {n_missing_analysis} missing-analysis tracks; see stderr]")

    n_stem_derivable = sum(1 for t in tracks if t.category == CATEGORY_STEM_DERIVABLE)
    if n_stem_derivable and not args.demucs_even_if_stems:
        print(
            f"[skipped {n_stem_derivable} stem-derivable tracks; run python -m apps.vocals from-stems --live]",
            file=sys.stderr,
        )

    rank = best_playlist_rank(ctx, tracks)
    queue = order_todo(
        _trickle_demucs_candidates(tracks, demucs_even_if_stems=bool(args.demucs_even_if_stems)),
        rank,
    )
    budget_s: float | None = None if args.max_minutes is None else float(args.max_minutes) * 60.0
    limit = int(args.limit)

    mode = "live" if live else "dry-run"
    print(
        f"[{mode}] up to {limit} of {len(queue)} demucs-gap track(s) "
        f"(ordered queue; --limit {limit})"
        + (f"; wall budget {_fmt_dur(budget_s)} (--max-minutes {args.max_minutes})" if budget_s is not None else "")
    )

    rate = DEMUCS_REALTIME_FACTOR
    loop_start = time.monotonic() if live else 0.0
    est_elapsed = 0.0
    started = 0
    completed = 0
    skipped_midrun = 0
    budget_skipped = 0
    failed = 0
    done_audio_s = 0.0
    done_wall_s = 0.0
    plan_index = 0

    for idx, tr in enumerate(queue):
        if started >= limit:
            break
        elapsed = (time.monotonic() - loop_start) if live else est_elapsed
        if budget_s is not None and not track_fits_budget(
            elapsed_s=elapsed,
            budget_s=budget_s,
            length_s=tr.length_s,
            rate=rate,
        ):
            remaining = max(0.0, budget_s - elapsed)
            est_s = tr.length_s * rate
            print(f"[SKIP budget] {tr.stable_id} {tr.title!r}: est {est_s:.0f}s, remaining {remaining:.0f}s")
            budget_skipped += 1
            continue

        plan_index += 1
        count, name = rank.get(tr.stable_id, (0, ""))
        pl = f"playlist={name!r}({count} on disk)" if count else "no playlist"
        print(f"  {plan_index}. {tr.stable_id} {tr.title!r} {tr.length_s}s {pl} est {_fmt_dur(tr.length_s * rate)}")

        if not live:
            started += 1
            est_elapsed += tr.length_s * rate
            continue

        if tr.audio_path is None or not tr.audio_path.is_file():
            print(f"[SKIP vanished] {tr.stable_id} {tr.title!r}: {tr.audio_path}")
            skipped_midrun += 1
            continue
        if vcache.load_valid_entry(vcache.cache_path(ctx.data_dir, tr.stable_id), tr.audio_path) is not None:
            print(f"[SKIP cached] {tr.stable_id} {tr.title!r}: valid cache entry")
            skipped_midrun += 1
            continue
        cache_file = vcache.cache_path(ctx.data_dir, tr.stable_id)
        claim = _claim_track(cache_file)
        if claim is None:
            print(f"[SKIP in-progress] {tr.stable_id} {tr.title!r}: another CLI owns it")
            skipped_midrun += 1
            continue

        started += 1
        print(f"[{started}/{limit}] {tr.stable_id} {tr.title!r} ({tr.length_s}s)")
        try:
            with _managed_track_claim(claim):
                if vcache.load_valid_entry(cache_file, tr.audio_path) is not None:
                    print(f"[SKIP cached] {tr.stable_id} {tr.title!r}: valid cache entry")
                    skipped_midrun += 1
                    started -= 1
                    continue
                wall_s, _entry = _process_one(
                    ctx,
                    tr,
                    f"[{started}/{limit}]",
                    args.worker_timeout_s,
                    claim,
                )
        except WorkerCleanupError:
            raise
        except Exception as exc:  # noqa: BLE001
            print(
                f"[FAIL] {tr.stable_id} {tr.title!r}: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            vattempts.record_failure(ctx.data_dir, tr.stable_id, exc)
            failed += 1
            continue

        completed += 1
        done_audio_s += tr.length_s
        done_wall_s += wall_s
        if done_audio_s:
            rate = done_wall_s / done_audio_s
        remaining_in_run = max(0, limit - started)
        remaining_audio_s = sum(t.length_s for t in queue[idx + 1 :][:remaining_in_run])
        print(
            f"        running: {completed} done, {remaining_in_run} left to start, "
            f"est remaining {_fmt_dur(remaining_audio_s * rate)} "
            f"(measured {rate:.2f}x realtime)"
        )

    if not live:
        print("[dry-run] no work performed; pass --live to execute")
        return 0

    wall_total = time.monotonic() - loop_start
    budget_note = ""
    if budget_s is not None and wall_total > budget_s:
        budget_note = f", budget exceeded by {_fmt_dur(wall_total - budget_s)}"
    print(
        f"[live] finished: {completed} analysed, "
        f"{skipped_midrun} skipped, {budget_skipped} budget-skipped, "
        f"{failed} failed, wall {_fmt_dur(wall_total)}"
        + (f", budget {_fmt_dur(budget_s)}{budget_note}" if budget_s is not None else "")
    )
    return 0


def cmd_one(args: argparse.Namespace) -> int:
    ctx = Ctx(data_dir=args.data_dir)
    tracks = [t for t in load_tracks(ctx, None) if t.stable_id == args.stable_id]
    if not tracks:
        raise SystemExit(f"error: stable_id {args.stable_id!r} has no rekordbox mapping in {ctx.state_db}")
    tr = tracks[0]
    if tr.audio_path is None or not tr.audio_path.is_file():
        raise SystemExit(f"error: audio file missing for {tr.stable_id}: {tr.folder_path or '(no FolderPath)'}")
    if _anlz_data_file(tr, load_path_map(ctx.data_dir)) is None:
        raise SystemExit(
            f"error: local ANLZ .DAT missing for {tr.stable_id}: "
            f"{tr.analysis_data_path or '(no AnalysisDataPath)'}; "
            "generated vocals would be unservable through /anlz"
        )
    cache_file = vcache.cache_path(ctx.data_dir, tr.stable_id)
    existing = vcache.load_valid_entry(cache_file, tr.audio_path)
    if existing is not None and not args.force:
        print(
            f"[cached] {tr.stable_id} {tr.title!r}: valid entry at "
            f"{cache_file} ({len(existing['regions'])} regions, "
            f"cov={existing['coverage_pct']}%); use --force to recompute"
        )
        return 0
    claim = _claim_track(cache_file)
    if claim is None:
        raise SystemExit(f"error: {tr.stable_id} is already being analysed by another vocals CLI process")
    with _managed_track_claim(claim):
        if not args.force and vcache.load_valid_entry(cache_file, tr.audio_path) is not None:
            print(f"[cached] {tr.stable_id} {tr.title!r}: valid entry at {cache_file}")
            return 0
        print(
            f"[one] {tr.stable_id} {tr.title!r} ({tr.length_s}s) est {_fmt_dur(tr.length_s * DEMUCS_REALTIME_FACTOR)}"
        )
        _process_one(ctx, tr, "[one]", args.worker_timeout_s, claim)
    return 0


# ----- parser ----------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    return build_vocals_parser(
        {
            "scan": cmd_scan,
            "trickle": cmd_trickle,
            "one": cmd_one,
            "from-stems": cmd_from_stems,
        }
    )


def main(argv: list[str] | None = None) -> int:
    from apps.vocals.playlist_cli_main import run_playlist_aware_cli

    return run_playlist_aware_cli(build_parser, argv=argv)


if __name__ == "__main__":
    sys.exit(main())
