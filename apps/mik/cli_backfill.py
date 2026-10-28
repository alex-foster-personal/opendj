"""``python -m apps.mik backfill status|plan|stage|collect|share``.

The command line of :mod:`apps.mik.backfill`. Each subcommand prints the same
JSON document its engine route (``/api/v1/energy``) and MCP tool
(``energy_*``) return. Everything is a dry run without ``--live``.

Exact command lines:
  python -m apps.mik backfill status --json
  python -m apps.mik backfill plan --limit 20 --json
  python -m apps.mik backfill stage --limit 100            (dry run)
  python -m apps.mik backfill stage --limit 100 --live
  python -m apps.mik backfill collect --batch <folder> --store <copy of the MIK store>
  python -m apps.mik backfill collect --batch <folder> --store <copy> --live --delete-copies
  python -m apps.mik backfill share --json                 (dry run)
  python -m apps.mik backfill share --live --json
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import availability as avail
from . import backfill, backfill_share
from .cli_common import _emit


def _library(args: argparse.Namespace) -> backfill.Library:
    return backfill.Library.at(avail.resolve_data_dir(args.data_dir))


def _path(raw: str | None) -> Path | None:
    return Path(raw).expanduser() if raw else None


def _coverage_lines(payload: dict[str, Any]) -> list[str]:
    without = payload["without_mik_energy"]
    other = sum(payload["energy_from_other_source"].values())
    staged = payload["staged_unmatched"]
    return [
        f"MIK energy against {payload['present']} present rows (denominator: present)",
        f"  with MIK energy: {payload['with_mik_energy']}   held by another source: {other}",
        f"  would gain by unique match: {without['would_gain_by_unique_match']}",
        f"  would gain from a complete donor: {without['would_gain_by_duplicate_sharing']} (run `backfill share`)",
        f"  would gain by loading a shared analysis: {without['would_gain_by_loading_shared_analysis']} "
        "(staged or store MIK rows; re-load that store with `python -m apps.mik load --store <store> --live`, "
        "share and promote cannot fill these)",
        f"  conflicting: {without['conflicting']}   "
        f"MIK energy but no series: {without['needs_series']}   "
        f"need a fresh analysis: {without['needs_fresh_analysis']}",
        "  staged unmatched energy rows: "
        + (", ".join(f"{k}={v}" for k, v in staged.items()) or "0"),
        "  equivalence gate: "
        + ", ".join(f"{k}={v}" for k, v in sorted(payload["equivalence"].items()))
        + "  (collect writes only fields that read passed)",
    ]


def _cmd_status(args: argparse.Namespace) -> int:
    payload = backfill.status(_library(args), share_across_duplicates=args.share_across_duplicates)
    _emit(payload, as_json=args.json, lines=_coverage_lines(payload))
    return 0


def _cmd_plan(args: argparse.Namespace) -> int:
    payload = backfill.plan(
        _library(args),
        limit=args.limit,
        store=_path(args.store),
        share_across_duplicates=args.share_across_duplicates,
        ingest_root=_path(args.ingest_root),
    )
    todo = payload["to_analyze"]
    lines = [
        *_coverage_lines(payload),
        f"  to analyze: {todo['files']} files settling {todo['rows']} rows; held back: "
        + ", ".join(f"{k}={v}" for k, v in sorted(todo["held_back"].items())),
        f"  next {len(payload['next'])}:",
        *(f"    {entry['source']}  ({len(entry['stable_ids'])} rows)" for entry in payload["next"]),
    ]
    _emit(payload, as_json=args.json, lines=lines)
    return 0


def _cmd_stage(args: argparse.Namespace) -> int:
    payload = backfill.stage(
        _library(args),
        limit=args.limit,
        live=args.live,
        store=_path(args.store),
        share_across_duplicates=args.share_across_duplicates,
        ingest_root=_path(args.ingest_root),
        min_free_bytes=args.min_free_bytes,
    )
    counts = payload["counts"]
    lines = [
        f"[{payload['mode']}] {counts['staged']} files ({payload['bytes']} bytes) -> {payload['batch_dir']}",
        "  held back: " + ", ".join(f"{k}={v}" for k, v in sorted(counts["held_back"].items())),
        f"  library root: {payload['library_root']}",
        "  " + (payload["open_command"] or "nothing to stage"),
    ]
    if not args.live:
        lines.append("  (dry-run: pass --live to copy)")
    _emit(payload, as_json=args.json, lines=lines)
    return 0


def _cmd_collect(args: argparse.Namespace) -> int:
    if not args.batch or not args.store:
        raise SystemExit("backfill collect needs --batch <folder> and --store <copy of the MIK store>")
    payload = backfill.collect(
        _library(args),
        backfill.CollectRequest(
            batch_dir=Path(args.batch).expanduser(),
            store=Path(args.store).expanduser(),
            library_root=_path(args.library_root),
            live=args.live,
            delete_copies=args.delete_copies,
            share_across_duplicates=args.share_across_duplicates,
            allow_unverified=args.i_know_equivalence_is_unverified,
            copy_store_first=args.copy_store_first,
        ),
    )
    batch, planned, copies = payload["sources_batch"], payload["planned"], payload["copies"]
    lines = [
        f"[{payload['mode']}] collect {payload['batch_dir']}",
        f"  resolved {batch['resolved']} of {batch['batch_songs']} batch songs, "
        f"{batch['unresolved']} unresolved, {batch['ignored_outside_batch']} outside the batch ignored",
        f"  planned: {planned['field_writes']} energy fields, {planned['segment_rows']} segment rows "
        f"across {planned['segment_tracks']} tracks",
        "  BLOCKED by equivalence gate: "
        + (", ".join(f"{k}={v}" for k, v in sorted(payload["blocked_by_gate"].items())) or "nothing"),
        f"  copies: {copies['deletable']} deletable, {copies['deleted']} deleted",
        "  applied: "
        + (
            ", ".join(f"{k}={v}" for k, v in sorted(payload["applied"].items()))
            if payload["applied"] is not None
            else "nothing (dry run)"
        ),
    ]
    lines += [f"    unresolved: {row['copy']}  {row['detail']}" for row in payload["unresolved"]]
    if not args.live:
        lines.append("  (dry-run: pass --live to write)")
    _emit(payload, as_json=args.json, lines=lines)
    return 0


def _cmd_share(args: argparse.Namespace) -> int:
    payload = backfill_share.share(
        _library(args),
        live=args.live,
        share_across_duplicates=args.share_across_duplicates,
        store=_path(args.store),
    )
    counts, planned = payload["counts"], payload["planned"]
    lines = [
        f"[{payload['mode']}] share: {counts['rows_to_fill']} rows to fill from {counts['donors']} complete donors "
        f"in {counts['groups']} groups; {counts['conflicting_groups']} groups conflicting (nothing written there)",
        f"  planned: {planned['field_writes']} energy fields, {planned['segment_tracks']} series",
        "  equivalence gate: " + ", ".join(f"{k}={v}" for k, v in sorted(payload["equivalence"].items())),
        "  applied: "
        + (
            ", ".join(f"{k}={v}" for k, v in sorted(payload["applied"].items()))
            if payload["applied"] is not None
            else "nothing (dry run)"
        ),
    ]
    if not args.live:
        lines.append("  (dry-run: pass --live to write)")
    _emit(payload, as_json=args.json, lines=lines)
    return 0


def add_backfill_parser(
    sub: argparse._SubParsersAction, common: Callable[[], argparse.ArgumentParser]
) -> None:
    """Attach ``backfill`` and its four steps under the top-level subparsers."""
    parent = sub.add_parser("backfill", help="give every present row a MIK energy: plan, stage, collect")
    steps = parent.add_subparsers(dest="backfill_step", required=True)
    for name, handler, help_text in (
        ("status", _cmd_status, "energy coverage against the present denominator"),
        ("plan", _cmd_plan, "files that need a fresh analysis, one per recording"),
        ("stage", _cmd_stage, "copy the next files into a batch folder (never launches MIK)"),
        ("collect", _cmd_collect, "load a batch's energy from a COPY of the MIK store"),
        ("share", _cmd_share, "give a complete donor's MIK energy and series to its empty duplicates"),
    ):
        child = steps.add_parser(name, help=help_text, parents=[common()])
        child.set_defaults(handler=handler)
        child.add_argument("--limit", type=int, default=None, help="plan/stage: how many files")
        child.add_argument(
            "--ingest-root",
            default=None,
            help="plan/stage: where batch folders live (default: the library's mik-backfill.json, else ~/Music/_ingest)",
        )
        child.add_argument(
            "--min-free-bytes",
            type=int,
            default=None,
            help="stage: free disk the copies must leave (default: the library's mik-backfill.json, else 10 GiB)",
        )
        child.add_argument("--batch", default=None, help="collect: the batch folder")
        child.add_argument(
            "--delete-copies",
            action="store_true",
            help="collect --live: remove the copies in the batch folder whose energy was loaded",
        )
        child.add_argument(
            "--copy-store-first",
            action="store_true",
            help="collect: --store is Mixed In Key's own file; copy it and read only the copy",
        )
