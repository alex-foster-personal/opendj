"""``python -m apps.mik load``: plan, and with ``--live`` write, a MIK store.

Split out of :mod:`apps.mik.cli` (file-size ceiling). Two ways to bind a MIK
song to a library row: the three tiers of :mod:`apps.mik.match` (an old store
whose paths are the library's own), or ``--sources-batch`` with
``--library-root`` (a fresh analysis of COPIES, bound through the batch
folder's ``.sources.txt`` by :mod:`apps.mik.sources_match`).
"""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from apps.shared.equivalence import verdict_path
from apps.shared.state import db as state_db

from . import availability as avail
from . import duplicates as dups
from . import load as loader
from . import match as matcher
from . import mikdb
from . import sources_match as sources
from .cli_common import UNVERIFIED_FLAG, _emit, _gate, _state_db_path


def _read_and_match(args: argparse.Namespace):
    """``match`` is read-only, and ``load`` is dry-run unless ``--live``. Open
    the state DB accordingly: ``open_rw`` applies pending schema migrations
    and the machine-ID backfill before returning, so opening it for a
    read-only or dry-run command would silently mutate ``state.db`` despite
    the command's own read-only/dry-run contract.
    """
    store = Path(args.store).expanduser() if args.store else mikdb.DEFAULT_STORE
    mik_conn = mikdb.open_ro(store)
    try:
        songs, read_stats = mikdb.read_songs(mik_conn)
    finally:
        mik_conn.close()
    path = _state_db_path(args)
    # Same open_dry_run rationale as cmd_availability: build_plan() below
    # queries v8-only tables (unmatched_source_analysis) even in dry-run, so
    # a plain open_ro against an un-migrated real database would raise
    # "no such table" while merely previewing --live.
    conn = state_db.open_rw(path) if args.live else state_db.open_dry_run(path)
    index = matcher.TrackIndex.from_conn(conn)
    report = matcher.match_songs(songs, index, allow_fuzzy=args.allow_fuzzy)
    return songs, read_stats, conn, index, report, store, path


def _sources_args(args: argparse.Namespace) -> tuple[Path, Path] | None:
    """``(batch folder, library root)`` for ``--sources-batch``, ``None`` for the
    three tiers. Checked before any store is read, so a half-given pair stops
    the command at once."""
    batch, root = args.sources_batch, args.library_root
    if batch is None and root is None:
        return None
    if batch is None or root is None:
        raise SystemExit("--sources-batch and --library-root go together: pass both or neither")
    return Path(batch).expanduser(), Path(root).expanduser()


# -------------------------------------------------------------------- load


def _load_plan_detail_lines(plan: loader.LoadPlan, applied: dict[str, Any] | None) -> list[str]:
    """The optional per-block detail lines for ``cmd_load``'s report, split
    out to keep it under the complexity ceiling: each block is independent
    and only appears when that condition on the plan actually fired."""
    lines: list[str] = []
    counted = (
        (plan.blocked_by_gate, "BLOCKED by equivalence gate", f"  (run unit B, or force with {UNVERIFIED_FLAG})"),
        (plan.not_selected, "not selected by --fields", ""),
        (plan.blocked_by_precedence, "skipped, a higher-precedence source already holds the field", ""),
        (plan.blocked_by_promotion_conflict, "skipped, already promoted to a different track", ""),
    )
    for counts, label, tail in counted:
        if counts:
            lines.append(f"  {label}: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) + tail)
    if plan.blocked_by_key_floor:
        lines.append(
            f"  skipped, MIK key confidence below "
            f"{loader.KEY_CONFIDENCE_FLOOR} (rekordbox wins outright): "
            f"{plan.blocked_by_key_floor}"
        )
    if plan.key_review_band:
        lines.append(
            f"  FLAGGED for review, MIK key confidence in the "
            f"{loader.KEY_CONFIDENCE_FLOOR}-{loader.KEY_CONFIDENCE_MIK_WINS} "
            f"band and rekordbox disagrees on ownership: "
            f"{len(plan.key_review_band)} tracks (never silently overwritten)"
        )
    if applied is not None:
        lines.append("  applied: " + ", ".join(f"{k}={v}" for k, v in sorted(applied.items())))
    else:
        lines.append("  (dry-run: pass --live to write)")
    return lines


def _rebind(
    args: argparse.Namespace, sources_args: tuple[Path, Path] | None, songs: list[mikdb.MikSong]
) -> dups.Rebind:
    """Re-run ``cmd_load``'s own matching against a (locked) connection."""

    def rebind(locked: sqlite3.Connection) -> tuple[list[mikdb.MikSong], matcher.MatchReport, Callable[[mikdb.MikSong], list[str]]]:
        if sources_args is not None:
            batch = sources.match_songs_by_sources(
                songs, locked, batch_dir=sources_args[0], library_root=sources_args[1], platform=sources.HOST_PLATFORM
            )
            return batch.songs, batch.report, batch.candidates_of
        index = matcher.TrackIndex.from_conn(locked)
        return (
            songs,
            matcher.match_songs(songs, index, allow_fuzzy=args.allow_fuzzy),
            dups.tier_candidates(index, allow_fuzzy=args.allow_fuzzy),
        )

    return rebind


def cmd_load(args: argparse.Namespace) -> int:
    sources_args = _sources_args(args)
    songs, _stats, conn, _index, report, store, path = _read_and_match(args)
    store_songs = list(songs)
    gate = _gate(args, mik_store=store)
    try:
        batch: sources.SourcesMatch | None = None
        candidates_of: Callable[[mikdb.MikSong], list[str]]
        if sources_args is not None:
            batch = sources.match_songs_by_sources(
                songs, conn, batch_dir=sources_args[0], library_root=sources_args[1], platform=sources.HOST_PLATFORM
            )
            songs, report = batch.songs, batch.report
            candidates_of = batch.candidates_of
        else:
            candidates_of = dups.tier_candidates(_index, allow_fuzzy=args.allow_fuzzy)
        share = dups.plan_sharing(
            songs,
            report,
            dups.duplicate_groups(conn, by_metadata=args.share_across_duplicates),
            candidates_of=candidates_of,
        )
        plan = loader.build_plan(
            songs,
            report,
            gate,
            conn,
            overwrite_lower_precedence=args.overwrite_lower_precedence,
            fields=args.fields,
            shared=share.shared,
        )
        plan.still_valid = dups.binding_unchanged(
            _rebind(args, sources_args, store_songs),
            report,
            share.shared,
            by_metadata=args.share_across_duplicates,
        )
        applied = None
        if args.live:
            result = loader.apply_plan(conn, plan)
            applied = {
                "fields_written": result.fields_written,
                "fields_unchanged": result.fields_unchanged,
                "segment_tracks": result.segment_tracks,
                "segment_rows": result.segment_rows,
                "segment_tracks_unchanged": result.segment_tracks_unchanged,
                "segment_rows_unchanged": result.segment_rows_unchanged,
                "staged_written": result.staged_written,
                "staged_unchanged": result.staged_unchanged,
                "stale_plan": result.stale_plan,
            }
        payload: dict[str, Any] = {
            "mode": "live" if args.live else "dry-run",
            "store": str(store),
            "state_db": str(path),
            "verdict_file": str(verdict_path(avail.resolve_data_dir(args.data_dir))),
            "verdict_file_present": gate.file_present,
            "equivalence": plan.gate_summary,
            "allow_unverified": gate.allow_unverified,
            "planned": {
                "field_writes": len(plan.field_writes),
                "segment_tracks": len(plan.segment_writes),
                "segment_rows": plan.segment_row_count,
                "staged_rows": len(plan.staged),
            },
            "blocked_by_gate": plan.blocked_by_gate,
            "not_selected": plan.not_selected,
            "blocked_by_precedence": plan.blocked_by_precedence,
            "blocked_by_promotion_conflict": plan.blocked_by_promotion_conflict,
            "blocked_by_key_confidence_floor": plan.blocked_by_key_floor,
            "key_review_band_flagged": len(plan.key_review_band),
            "source_had_no_value": plan.no_value,
            "applied": applied,
            "sources_batch": batch.payload() if batch is not None else None,
            "duplicate_sharing": {
                **share.counts(),
                "by_artist_title_duration": args.share_across_duplicates,
            },
        }
    finally:
        conn.close()
    lines = [
        f"[{payload['mode']}] MIK load into {path}",
        f"  verdict file: {payload['verdict_file']} "
        f"({'present' if gate.file_present else 'ABSENT -> all fields untested'})",
        ("  equivalence: " + ", ".join(f"{k}={v}" for k, v in sorted(plan.gate_summary.items()))),
        f"  planned: {payload['planned']['field_writes']} track_fields, "
        f"{payload['planned']['segment_rows']} segment rows across "
        f"{payload['planned']['segment_tracks']} tracks, "
        f"{payload['planned']['staged_rows']} unmatched_source_analysis rows",
    ]
    if batch is not None:
        counts = batch.counts()
        lines.append(
            f"  sources map: {counts['resolved']} of {counts['batch_songs']} batch songs resolved, "
            f"{counts['unresolved']} unresolved ("
            + ", ".join(f"{k}={v}" for k, v in sorted(batch.by_detail().items()))
            + f"), {counts['ignored_outside_batch']} store songs outside the batch ignored"
        )
    sharing = share.counts()
    lines.append(
        f"  duplicate sharing: {sharing['rows_shared']} extra rows from "
        f"{sharing['songs_sharing']} songs, {sharing['conflicting_rows']} rows with "
        f"conflicting energies (not written), {sharing['ambiguous_not_one_recording']} "
        "ambiguous songs whose candidates are not one recording"
        + ("" if args.share_across_duplicates else "  (identity hashes only; add --share-across-duplicates)")
    )
    lines.extend(_load_plan_detail_lines(plan, applied))
    _emit(payload, as_json=args.json, lines=lines)
    return 0

