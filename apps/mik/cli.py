"""``python -m apps.mik`` - MIK analysis retention CLI.

the maintainer, Tue 28 Jul 2026: "can we still grab the analyses of the files we don't
have but mark them file missing? maybe in a different table to prevent
confusion later, doubt db schema will change much."

Requirements (mini-PRD):
  ✔︎ ✅ ``availability``: classify every ``tracks`` row as
    present / absent / awaiting_volume / streaming and write
    ``track_availability``. DRY-RUN by default, ``--live`` writes. Idempotent:
    a second run reports 0 changed.
    [if] a path resolves on disk [then] present
    [if] a path is under an UNMOUNTED /Volumes/<name> [then] awaiting_volume,
      never absent (plugging the drive in makes it present again)
    [if] counts do not sum to the tracks row count [then ⛔️]
  ✔︎ ✅ ``read``: read Collection10.mikdb READ-ONLY and report what MIK holds,
    including every value the reader REFUSED and why. No DB writes at all.
    [if] a ZBOOKMARKDATA blob is parsed by ASCII scrape [then ⛔️] the
      0x0101 record parser is mandatory (an ASCII scrape gives a FALSE zero
      match)
    [if] any song has 2+ ZKEYSEGMENT rows [then ⛔️] ZCONFIDENCE would not be
      a per-track scalar
  ✔︎ ✅ ``match``: three-tier match report (exact_path / basename /
    artist_title) with ambiguity and collision counts. Read-only.
    [if] one MIK song matches 2+ tracks in its winning tier [then] it is
      ambiguous and gets NO stable_id
    [if] 2+ MIK songs match one track [then] deterministic winner by
      (tier, key confidence, row id); losers are lost_collision
  ✔︎ ✅ ``load``: plan + optionally write scalars, the energy time series and
    the unmatched staging rows. DRY-RUN by default, ``--live`` writes.
    EVERY field is gated on the equivalence verdict file
    (``data/state/equivalence-verdicts.json``); absent file means every field
    is untested and NOTHING is written.
    [if] the verdict file is absent and no override flag [then] zero rows
      written, and stdout says so per field
    [if] ``--i-know-equivalence-is-unverified`` [then] a WARNING per field
      naming the field and its status
    [if] rekordbox already supplied ``bpm`` [then] MIK's bpm is skipped
      (MIK loses on BPM; the PK has no source column so a write clobbers)
    [if] a MIK key has ZCONFIDENCE below 0.70 [then] skipped, falls through
      to rekordbox
  ✔︎ ✅ ``promote``: move staged rows onto a track once its identity is known.
    ``--discover`` re-matches pending rows against ``tracks`` as it is now.
    DRY-RUN by default.
    [if] the same source row is promoted twice [then] the second run writes
      nothing and reports already_promoted
    [if] a staged row was promoted to a DIFFERENT stable_id [then ⛔️]
      PromotionConflict, never a silent re-point
  ✔︎ ✅ ``--json`` on every subcommand (agent-native parity), and
    ``--data-dir`` so a worktree can point at the primary checkout's data.
  → enabling ``apps/tags/collect.py::default_fetch_mik`` and the
    ``apps/tags/unify.py`` precedence corrections: OUT OF SCOPE here, separate
    PR. This module deliberately only provides the reader + loader they need.

Exact command lines:
  python -m apps.mik availability --data-dir /Users/user/code/music-dj-tools/data
  python -m apps.mik read --json
  python -m apps.mik match --data-dir /Users/user/code/music-dj-tools/data
  python -m apps.mik load --data-dir /Users/user/code/music-dj-tools/data
  python -m apps.mik load --data-dir /Users/user/code/music-dj-tools/data --live
  python -m apps.mik promote --discover --data-dir /Users/user/code/music-dj-tools/data

-Claude
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

from apps.shared.equivalence import verdict_path
from apps.shared.mik_energy import readable_mik_energy
from apps.shared.scan_mass_missing import MassMissingError
from apps.shared.state import db as state_db

from . import availability as avail
from . import load as loader
from . import match as matcher
from . import mikdb
from .cli_common import UNVERIFIED_FLAG, _emit, _gate, _state_db_path
from .cli_promote import cmd_promote

# ------------------------------------------------------------ availability


def cmd_availability(args: argparse.Namespace) -> int:
    path = _state_db_path(args)
    # open_rw applies pending schema migrations and the machine-ID backfill
    # before returning, so opening it here for a dry-run would silently
    # mutate state.db despite "everything is dry-run without --live".
    # open_dry_run migrates a disposable sibling-file copy instead, so a dry run against
    # an old (pre-v8) database can still query the v8-only track_availability
    # table without touching the real file.
    conn = state_db.open_rw(path) if args.live else state_db.open_dry_run(path)
    allow = bool(getattr(args, "allow_mass_missing", False))
    try:
        rows = avail.probe(conn)
        histogram: dict[str, int] = {}
        for row in rows:
            histogram[row.state] = histogram.get(row.state, 0) + 1
        if args.live:
            report = avail.write(conn, rows, allow_mass_missing=allow)
            written = {"changed": report.changed, "unchanged": report.unchanged}
        else:
            avail.guard_present_drop(conn, rows, allow_mass_missing=allow)
            written = {"changed": 0, "unchanged": 0}
        payload: dict[str, Any] = {
            "mode": "live" if args.live else "dry-run",
            "state_db": str(path),
            "tracks": len(rows),
            "classified": histogram,
            "written": written,
            "stored": avail.counts(conn),
        }
    except MassMissingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    total = sum(payload["classified"].values())
    if total != payload["tracks"]:
        raise SystemExit(f"classified {total} rows but tracks has {payload['tracks']}")
    _emit(
        payload,
        as_json=args.json,
        lines=[
            f"[{payload['mode']}] {payload['tracks']} tracks in {path}",
            ("  classified: " + ", ".join(f"{k}={v}" for k, v in sorted(histogram.items()))),
            (
                "  stored:     "
                + ", ".join(f"{k}={v}" for k, v in sorted(payload["stored"].items()))
            ),
            f"  written:    changed={written['changed']} "
            f"unchanged={written['unchanged']}"
            + ("" if args.live else "  (dry-run: pass --live to write)"),
        ],
    )
    return 0


# -------------------------------------------------------------------- read


def _song_presence_counts(songs: list[mikdb.MikSong]) -> dict[str, int]:
    """Half of ``_song_field_counts``, split further to keep each half under
    the complexity ceiling (radon charges one branch per comprehension
    ``if``, so six of these in one function was already over budget)."""
    return {
        "with_path": sum(1 for song in songs if song.path),
        "with_key": sum(1 for song in songs if song.key_camelot),
        "with_key_confidence": sum(1 for song in songs if song.key_confidence is not None),
    }


def _song_measurement_counts(songs: list[mikdb.MikSong]) -> dict[str, int]:
    """The other half of ``_song_field_counts``. See its sibling above."""
    return {
        "with_energy": sum(1 for song in songs if song.energy is not None),
        "with_bpm": sum(1 for song in songs if song.bpm is not None),
        "with_loudness": sum(1 for song in songs if song.loudness is not None),
        "segment_rows": sum(len(song.segments) for song in songs),
        "songs_with_segments": sum(1 for song in songs if song.segments),
    }


def _song_field_counts(songs: list[mikdb.MikSong]) -> dict[str, int]:
    """The per-field presence tallies of ``_read_stats_payload``, split out
    to keep it under the complexity ceiling (each is a plain count, not a
    decision)."""
    return {**_song_presence_counts(songs), **_song_measurement_counts(songs)}


def _read_stats_payload(
    store: Path, songs: list[mikdb.MikSong], stats: mikdb.ReadStats
) -> dict[str, Any]:
    """Builds ``cmd_read``'s report payload, split out to keep it under the
    complexity ceiling (each summary is a plain count, not a decision)."""
    return {
        "store": str(store),
        "songs": stats.songs,
        **_song_field_counts(songs),
        "rejected": {
            "bookmark_missing": stats.bookmark_missing,
            "bookmark_unparseable": stats.bookmark_unparseable,
            "key": stats.key_rejected,
            "energy": stats.energy_rejected,
            "bpm": stats.bpm_rejected,
            "loudness": stats.loudness_rejected,
            "key_confidence": stats.confidence_rejected,
            "segments": stats.segments_rejected,
        },
        "segments_clamped_negative_start": stats.segments_clamped_negative_start,
        "reasons": stats.reasons,
    }


def cmd_read(args: argparse.Namespace) -> int:
    store = Path(args.store).expanduser() if args.store else mikdb.DEFAULT_STORE
    conn = mikdb.open_ro(store)
    try:
        songs, stats = mikdb.read_songs(conn)
    finally:
        conn.close()
    payload = _read_stats_payload(store, songs, stats)
    _emit(
        payload,
        as_json=args.json,
        lines=[
            f"{payload['songs']} songs in {store} (READ-ONLY)",
            f"  paths parsed from bookmark blobs: {payload['with_path']}",
            f"  key={payload['with_key']} conf={payload['with_key_confidence']} "
            f"energy={payload['with_energy']} bpm={payload['with_bpm']} "
            f"loudness={payload['with_loudness']}",
            f"  energy series: {payload['segment_rows']} segments across "
            f"{payload['songs_with_segments']} songs",
            (
                "  rejected: "
                + ", ".join(f"{k}={v}" for k, v in sorted(payload["rejected"].items()))
            ),
        ]
        + [f"    {reason}: {count}" for reason, count in sorted(stats.reasons.items())],
    )
    return 0


# ------------------------------------------------------------------- match


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


def cmd_match(args: argparse.Namespace) -> int:
    songs, _stats, conn, index, report, store, path = _read_and_match(args)
    try:
        payload = {
            "store": str(store),
            "state_db": str(path),
            "tracks_indexed": index.track_count,
            "mik_songs": len(songs),
            "matched": len(report.matches),
            "distinct_tracks_matched": len({m.stable_id for m in report.matches.values()}),
            "by_tier": report.by_tier(),
            "rows_with_candidates_per_tier": report.tier_candidate_counts(),
            "unmatched": len(report.unmatched),
            "by_reason": report.by_reason(),
            "allow_fuzzy": args.allow_fuzzy,
        }
    finally:
        conn.close()
    _emit(
        payload,
        as_json=args.json,
        lines=[
            f"{payload['mik_songs']} MIK songs vs {payload['tracks_indexed']} tracks",
            f"  matched {payload['matched']} -> "
            f"{payload['distinct_tracks_matched']} distinct tracks",
            ("  by tier: " + ", ".join(f"{k}={v}" for k, v in payload["by_tier"].items())),
            (
                f"  unmatched {payload['unmatched']}: "
                + ", ".join(f"{k}={v}" for k, v in sorted(payload["by_reason"].items()))
            ),
        ],
    )
    return 0


# ---------------------------------------------------------------- coverage


def cmd_coverage(args: argparse.Namespace) -> int:
    """Measure the library's usable MIK 1-9 energy coverage without writes."""
    songs, _stats, conn, index, report, store, path = _read_and_match(args)
    try:
        readable = {
            match.stable_id
            for song_id, match in report.matches.items()
            if readable_mik_energy(songs[song_id].energy) is not None
        }
        tracks_matched = len({match.stable_id for match in report.matches.values()})
        payload = {
            "mode": "read-only",
            "store": str(store),
            "state_db": str(path),
            "tracks_indexed": index.track_count,
            "tracks_matched": tracks_matched,
            "tracks_with_readable_mik_energy": len(readable),
            "fraction": len(readable) / tracks_matched if tracks_matched else 0.0,
            "scale": "1-9",
        }
    finally:
        conn.close()
    _emit(
        payload,
        as_json=args.json,
        lines=[
            f"{payload['tracks_with_readable_mik_energy']} of {tracks_matched} matched "
            "tracks have readable MIK energy (1-9)",
            f"  fraction: {payload['fraction']:.2%}",
            f"  MIK store: {store}",
        ],
    )
    return 0


# -------------------------------------------------------------------- load


def _load_plan_detail_lines(plan: loader.LoadPlan, applied: dict[str, Any] | None) -> list[str]:
    """The optional per-block detail lines for ``cmd_load``'s report, split
    out to keep it under the complexity ceiling: each block is independent
    and only appears when that condition on the plan actually fired."""
    lines: list[str] = []
    if plan.blocked_by_gate:
        lines.append(
            "  BLOCKED by equivalence gate: "
            + ", ".join(f"{k}={v}" for k, v in sorted(plan.blocked_by_gate.items()))
            + f"  (run unit B, or force with {UNVERIFIED_FLAG})"
        )
    if plan.blocked_by_precedence:
        lines.append(
            "  skipped, a higher-precedence source already holds the field: "
            + ", ".join(f"{k}={v}" for k, v in sorted(plan.blocked_by_precedence.items()))
        )
    if plan.blocked_by_promotion_conflict:
        lines.append(
            "  skipped, already promoted to a different track: "
            + ", ".join(f"{k}={v}" for k, v in sorted(plan.blocked_by_promotion_conflict.items()))
        )
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


def cmd_load(args: argparse.Namespace) -> int:
    songs, _stats, conn, _index, report, store, path = _read_and_match(args)
    gate = _gate(args, mik_store=store)
    try:
        plan = loader.build_plan(
            songs,
            report,
            gate,
            conn,
            overwrite_lower_precedence=args.overwrite_lower_precedence,
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
            "blocked_by_precedence": plan.blocked_by_precedence,
            "blocked_by_promotion_conflict": plan.blocked_by_promotion_conflict,
            "blocked_by_key_confidence_floor": plan.blocked_by_key_floor,
            "key_review_band_flagged": len(plan.key_review_band),
            "source_had_no_value": plan.no_value,
            "applied": applied,
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
    lines.extend(_load_plan_detail_lines(plan, applied))
    _emit(payload, as_json=args.json, lines=lines)
    return 0


# --------------------------------------------------------------- argparse


def _common_options() -> argparse.ArgumentParser:
    """Shared flags, attached to EVERY subparser (and not to the top parser).

    Deliberately subcommand-scoped: argparse lets a subparser's defaults
    overwrite values already parsed by the top parser, so declaring the same
    flag in both places makes ``--live load`` silently mean ``live=False``.
    One home per flag, always after the subcommand.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--data-dir",
        default=None,
        help="data/ root holding state/state.db (worktrees pass the primary "
        "checkout's data dir explicitly)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--store",
        default=None,
        help=f"MIK store path (default {mikdb.DEFAULT_STORE}); opened READ-ONLY",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="actually write. Everything is dry-run without it",
    )
    parser.add_argument(
        "--allow-fuzzy",
        dest="allow_fuzzy",
        action="store_true",
        default=True,
        help="use the basename and artist+title tiers (default on)",
    )
    parser.add_argument(
        "--exact-path-only",
        dest="allow_fuzzy",
        action="store_false",
        help="exact-path tier only: the honest floor, no filename heuristics",
    )
    parser.add_argument(
        "--overwrite-lower-precedence",
        action="store_true",
        help="let a MIK key above the confidence floor replace an existing "
        "rekordbox key. Off by default: track_fields is keyed without a "
        "source column, so a write CLOBBERS",
    )
    parser.add_argument(
        UNVERIFIED_FLAG,
        dest="i_know_equivalence_is_unverified",
        action="store_true",
        help="write fields whose equivalence test has NOT passed. Logs loudly "
        "per field. Never a default",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="promote only: re-match pending staged rows against tracks as it is now",
    )
    parser.add_argument("--source-row-id", default=None, help="promote only")
    parser.add_argument("--stable-id", default=None, help="promote only")
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m apps.mik",
        description=(
            "Retain Mixed In Key analysis, including for audio we do not have. "
            "All flags go AFTER the subcommand."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, handler, help_text in (
        ("availability", cmd_availability, "classify + record audio availability"),
        ("read", cmd_read, "report what MIK holds (read-only, no DB writes)"),
        ("match", cmd_match, "three-tier match report"),
        ("coverage", cmd_coverage, "measure readable MIK energy coverage"),
        ("load", cmd_load, "plan/write scalars, energy series and staging rows"),
        ("promote", cmd_promote, "staged analysis -> track_fields"),
    ):
        child = sub.add_parser(name, help=help_text, parents=[_common_options()])
        child.set_defaults(handler=handler)
        if name == "availability":
            child.add_argument(
                "--allow-mass-missing",
                action="store_true",
                help="override LIBM-41: allow a scan that drops more than "
                "50% of previously present files (including to zero)",
            )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
