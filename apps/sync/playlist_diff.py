"""CLI + artefact writers for Phase 3 playlist diff (SYNC-03).

Reads the RB working-copy DB, the djay working-copy DB, and the Phase 2
``matches.csv`` file; then emits:

* ``data/sync/playlist-diff.md`` -- human-readable summary.
* ``data/sync/playlist-patch.csv`` -- flat ops table.
* ``data/sync/playlist-plan.json`` -- structured plan (apply step input).

This module does NOT touch live DBs. It only opens the working copies
produced by :func:`apps.shared.paths.copy_live_dbs`.
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from apps.shared import paths
from apps.shared.rekordbox_db import (
    RBPlaylist,
    is_streaming_path,
    iter_playlists as rb_iter_playlists,
    iter_tracks as rb_iter_tracks,
    open_db as rb_open_db,
)
from apps.shared.djay_db import iter_playlists as djay_iter_playlists
from apps.sync import playlist_plan as pp


DEFAULT_OUT_DIR: Path = paths.DATA_DIR / "sync"
DEFAULT_MATCHES: Path = DEFAULT_OUT_DIR / "matches.csv"

#: Safety ceiling -- if a plan proposes more ops than this we abort with a
#: hint. CONTEXT D4 mentions 10,000.
MAX_OPS_BEFORE_ABORT = 10_000


# ----- Artefact writers --------------------------------------------------


def _plan_to_jsonable(plan: pp.PlaylistPlan) -> dict:
    """Return a plain-dict projection of ``plan`` suitable for ``json.dump``."""
    return {
        "generated_at": plan.generated_at,
        "match_set_sha256": plan.match_set_sha256,
        "playlists": [
            {
                "rb_id": op.rb_id,
                "rb_name": op.rb_name,
                "op": op.op,
                "djay_uuid": op.djay_uuid,
                "djay_name_current": op.djay_name_current,
                "target_members": [asdict(m) for m in op.target_members],
                "djay_current_members": list(op.djay_current_members),
                "adds": [asdict(m) for m in op.adds],
                "removes": list(op.removes),
                "reordered": op.reordered,
                "unmatched_rb": [asdict(u) for u in op.unmatched_rb],
            }
            for op in plan.playlists
        ],
        "djay_only": [asdict(d) for d in plan.djay_only],
    }


def write_plan_json(plan: pp.PlaylistPlan, out_path: Path) -> None:
    """Dump the structured plan to ``out_path`` (pretty + stable)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(_plan_to_jsonable(plan), indent=2, sort_keys=False, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )


def write_patch_csv(plan: pp.PlaylistPlan, out_path: Path) -> None:
    """Dump a flat ops table per CONTEXT D4."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "op",
        "playlist_name",
        "playlist_rb_id",
        "playlist_djay_uuid",
        "track_rb_id",
        "track_djay_uuid",
        "new_track_no",
        "reason",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for op in plan.playlists:
            base = {
                "playlist_name": op.rb_name,
                "playlist_rb_id": op.rb_id,
                "playlist_djay_uuid": op.djay_uuid or "",
            }
            if op.op == "create":
                writer.writerow(
                    {
                        **base,
                        "op": "create_playlist",
                        "track_rb_id": "",
                        "track_djay_uuid": "",
                        "new_track_no": "",
                        "reason": "rb_playlist_absent_in_djay",
                    }
                )
                for m in op.adds:
                    writer.writerow(
                        {
                            **base,
                            "op": "add_member",
                            "track_rb_id": m.rb_id,
                            "track_djay_uuid": m.djay_uuid,
                            "new_track_no": str(m.track_no),
                            "reason": "initial_membership",
                        }
                    )
            elif op.op == "update":
                for m in op.adds:
                    writer.writerow(
                        {
                            **base,
                            "op": "add_member",
                            "track_rb_id": m.rb_id,
                            "track_djay_uuid": m.djay_uuid,
                            "new_track_no": str(m.track_no),
                            "reason": "rb_has_extra_track",
                        }
                    )
                for u in op.removes:
                    writer.writerow(
                        {
                            **base,
                            "op": "remove_member",
                            "track_rb_id": "",
                            "track_djay_uuid": u,
                            "new_track_no": "",
                            "reason": "djay_has_extra_track_not_in_rb",
                        }
                    )
                if op.reordered and not op.adds and not op.removes:
                    writer.writerow(
                        {
                            **base,
                            "op": "reorder",
                            "track_rb_id": "",
                            "track_djay_uuid": "",
                            "new_track_no": "",
                            "reason": "rb_ordering_differs",
                        }
                    )
            else:
                writer.writerow(
                    {
                        **base,
                        "op": "noop",
                        "track_rb_id": "",
                        "track_djay_uuid": "",
                        "new_track_no": "",
                        "reason": "already_in_sync",
                    }
                )
        # djay-only section
        for d in plan.djay_only:
            writer.writerow(
                {
                    "op": "noop",
                    "playlist_name": d.name,
                    "playlist_rb_id": "",
                    "playlist_djay_uuid": d.djay_uuid,
                    "track_rb_id": "",
                    "track_djay_uuid": "",
                    "new_track_no": "",
                    "reason": "djay_only_playlist_left_alone",
                }
            )


def _md_escape(s: str) -> str:
    return s.replace("|", "\\|")


def write_diff_md(plan: pp.PlaylistPlan, out_path: Path) -> None:
    """Write a markdown summary with rich-style tables."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append("# Playlist sync dry-run (SYNC-03)")
    lines.append("")
    lines.append(f"* Generated at: `{plan.generated_at or 'unknown'}`")
    lines.append(f"* Match-set sha256: `{plan.match_set_sha256 or 'none'}`")

    totals = {"create": 0, "update": 0, "noop": 0}
    for op in plan.playlists:
        totals[op.op] = totals.get(op.op, 0) + 1
    total_adds = sum(len(op.adds) for op in plan.playlists)
    total_removes = sum(len(op.removes) for op in plan.playlists)
    total_unmatched = sum(len(op.unmatched_rb) for op in plan.playlists)
    lines.append(
        f"* Totals: {totals.get('create', 0)} create, "
        f"{totals.get('update', 0)} update, "
        f"{totals.get('noop', 0)} noop, "
        f"{len(plan.djay_only)} djay-only, "
        f"{total_adds} adds, {total_removes} removes, "
        f"{total_unmatched} unmatched-rb-tracks."
    )
    lines.append("")
    lines.append("## RB -> djay operations")
    lines.append("")
    lines.append("| op | rb_name | rb_id | djay_uuid | adds | removes | reordered | unmatched |")
    lines.append("|---|---|---|---|---:|---:|:-:|---:|")
    for op in plan.playlists:
        lines.append(
            "| {op} | {name} | `{rid}` | `{duuid}` | {a} | {r} | {ro} | {u} |".format(
                op=op.op,
                name=_md_escape(op.rb_name),
                rid=op.rb_id,
                duuid=op.djay_uuid or "-",
                a=len(op.adds),
                r=len(op.removes),
                ro="yes" if op.reordered else "no",
                u=len(op.unmatched_rb),
            )
        )
    lines.append("")

    # Per-playlist detail sections.
    any_detail = False
    for op in plan.playlists:
        if op.op == "noop" and not op.unmatched_rb:
            continue
        any_detail = True
        lines.append(f"## {op.rb_name}")
        lines.append("")
        lines.append(f"* op: `{op.op}`")
        lines.append(f"* rb_id: `{op.rb_id}`")
        lines.append(f"* djay_uuid: `{op.djay_uuid or '(new)'}`")
        lines.append(
            f"* target_members: {len(op.target_members)}, adds: {len(op.adds)}, "
            f"removes: {len(op.removes)}, reordered: {op.reordered}, "
            f"unmatched_rb: {len(op.unmatched_rb)}"
        )
        if op.adds:
            lines.append("")
            lines.append("### Adds")
            lines.append("")
            lines.append("| track_no | rb_id | djay_uuid |")
            lines.append("|---:|---|---|")
            for m in op.adds:
                lines.append(f"| {m.track_no} | `{m.rb_id}` | `{m.djay_uuid}` |")
        if op.removes:
            lines.append("")
            lines.append("### Removes (djay had, RB does not)")
            lines.append("")
            lines.append("| djay_uuid |")
            lines.append("|---|")
            for u in op.removes:
                lines.append(f"| `{u}` |")
        if op.unmatched_rb:
            lines.append("")
            lines.append("### Unmatched RB tracks (excluded from write)")
            lines.append("")
            lines.append("| rb_id | title | artist |")
            lines.append("|---|---|---|")
            for u in op.unmatched_rb:
                lines.append(
                    f"| `{u.rb_id}` | {_md_escape(u.title)} | {_md_escape(u.artist)} |"
                )
        lines.append("")
    if not any_detail:
        lines.append("_All playlists in sync; no detail sections needed._")
        lines.append("")

    lines.append("## djay-only playlists (left alone per D3)")
    lines.append("")
    if plan.djay_only:
        lines.append("| djay_uuid | name | member_count |")
        lines.append("|---|---|---:|")
        for d in plan.djay_only:
            lines.append(
                f"| `{d.djay_uuid}` | {_md_escape(d.name)} | {d.member_count} |"
            )
    else:
        lines.append("_None._")
    lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")


# ----- Plan pipeline -----------------------------------------------------


def _load_rb_inputs(
    rb_db_path: Path | None,
) -> tuple[list[RBPlaylist], dict[str, bool], dict[str, tuple[str, str]]]:
    """Return (playlists, streaming_filter, titles-by-rb-id) from the RB DB."""
    db = rb_open_db(rb_db_path)
    try:
        tracks = list(rb_iter_tracks(db))
        playlists = list(rb_iter_playlists(db))
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass
    streaming: dict[str, bool] = {
        t.id: (t.is_streaming or is_streaming_path(t.folder_path)) for t in tracks
    }
    titles: dict[str, tuple[str, str]] = {t.id: (t.title, t.artist) for t in tracks}
    return playlists, streaming, titles


def _path_under_repo(p: Path) -> bool:
    """True if ``p`` resolves to a path beneath the repo root (safety rail)."""
    try:
        resolved = p.resolve()
        root = paths.PROJECT_ROOT.resolve()
        return os.path.commonpath([str(resolved), str(root)]) == str(root)
    except (ValueError, OSError):
        return False


def generate_plan(
    *,
    rb_db_path: Path | None,
    djay_db_path: Path | None,
    matches_path: Path,
    only_playlists: set[str] | None = None,
    generated_at: str | None = None,
) -> pp.PlaylistPlan:
    """High-level helper: load inputs, build the plan, return it."""
    rb_playlists, streaming, titles = _load_rb_inputs(rb_db_path)
    flat = pp.flatten_rb_playlists(rb_playlists, streaming_filter=streaming)
    if only_playlists:
        # Compare on canonical name so users can specify with their own casing.
        wanted = {pp._canonical_name(n) for n in only_playlists}
        flat = [f for f in flat if pp._canonical_name(f.flat_name) in wanted]

    djay_path = djay_db_path or paths.DJAY_WORKING_DB
    if not djay_path.exists():
        copied = paths.copy_live_dbs()
        resolved = copied.get("djay")
        if resolved is None:
            raise FileNotFoundError(
                f"djay working DB not at {djay_path} and live DB "
                f"{paths.DJAY_LIVE_DB} is missing."
            )
        djay_path = resolved
    djay_playlists = pp.read_djay_playlists(djay_iter_playlists(djay_path))

    matches = pp.load_match_set(matches_path)
    plan = pp.build_plan(
        flat,
        djay_playlists,
        matches,
        rb_track_titles=titles,
        generated_at=generated_at or _dt.datetime.now(_dt.UTC).isoformat(),
    )
    return plan


# ----- CLI ---------------------------------------------------------------


def _print_summary(plan: pp.PlaylistPlan) -> None:
    try:
        from rich.console import Console
        from rich.table import Table
    except ImportError:  # pragma: no cover - rich is a project dep
        Console = None
        Table = None

    totals = {"create": 0, "update": 0, "noop": 0}
    for op in plan.playlists:
        totals[op.op] = totals.get(op.op, 0) + 1
    total_adds = sum(len(op.adds) for op in plan.playlists)
    total_removes = sum(len(op.removes) for op in plan.playlists)
    total_unmatched = sum(len(op.unmatched_rb) for op in plan.playlists)

    if Console is None:  # pragma: no cover
        print(
            f"create={totals.get('create', 0)} "
            f"update={totals.get('update', 0)} "
            f"noop={totals.get('noop', 0)} "
            f"djay_only={len(plan.djay_only)} "
            f"adds={total_adds} removes={total_removes} unmatched={total_unmatched}"
        )
        return

    console = Console(width=120)
    summary = Table(title="Playlist sync plan (dry-run)")
    summary.add_column("metric")
    summary.add_column("value", justify="right")
    summary.add_row("RB create ops", str(totals.get("create", 0)))
    summary.add_row("RB update ops", str(totals.get("update", 0)))
    summary.add_row("RB noop ops", str(totals.get("noop", 0)))
    summary.add_row("djay-only playlists", str(len(plan.djay_only)))
    summary.add_row("membership adds", str(total_adds))
    summary.add_row("membership removes", str(total_removes))
    summary.add_row("unmatched rb tracks", str(total_unmatched))
    console.print(summary)

    top = sorted(
        plan.playlists,
        key=lambda o: len(o.adds) + len(o.removes) + (1 if o.reordered else 0),
        reverse=True,
    )[:5]
    if top:
        table = Table(title="Top 5 playlists by op count")
        table.add_column("rb_name")
        table.add_column("op")
        table.add_column("adds", justify="right")
        table.add_column("removes", justify="right")
        table.add_column("reordered")
        table.add_column("unmatched", justify="right")
        for op in top:
            table.add_row(
                op.rb_name,
                op.op,
                str(len(op.adds)),
                str(len(op.removes)),
                "yes" if op.reordered else "no",
                str(len(op.unmatched_rb)),
            )
        console.print(table)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="apps.sync.playlist_diff",
        description=(
            "Compute the Phase 3 RB-canonical playlist diff against djay Pro "
            "and emit playlist-diff.md + playlist-patch.csv + playlist-plan.json."
        ),
    )
    parser.add_argument("--rb-db", type=Path, default=None)
    parser.add_argument("--djay-db", type=Path, default=None)
    parser.add_argument("--matches", type=Path, default=DEFAULT_MATCHES)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument(
        "--only-playlists",
        default=None,
        help="comma-separated canonical playlist names to restrict the plan to",
    )
    parser.add_argument(
        "--max-ops",
        type=int,
        default=MAX_OPS_BEFORE_ABORT,
        help="abort when the plan would contain more than this many ops",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if not _path_under_repo(args.out_dir):
        sys.stderr.write(
            f"refusing to write outside repo root: {args.out_dir}\n"
        )
        return 2

    only: set[str] | None = None
    if args.only_playlists:
        only = {s.strip() for s in args.only_playlists.split(",") if s.strip()}

    try:
        plan = generate_plan(
            rb_db_path=args.rb_db,
            djay_db_path=args.djay_db,
            matches_path=args.matches,
            only_playlists=only,
        )
    except FileNotFoundError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2

    op_total = (
        sum(len(op.adds) + len(op.removes) + (1 if op.op == "create" else 0) for op in plan.playlists)
    )
    if op_total > args.max_ops:
        sys.stderr.write(
            f"plan contains {op_total} ops (> --max-ops={args.max_ops}); "
            "narrow scope with --only-playlists.\n"
        )
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_plan_json(plan, args.out_dir / "playlist-plan.json")
    write_patch_csv(plan, args.out_dir / "playlist-patch.csv")
    write_diff_md(plan, args.out_dir / "playlist-diff.md")
    _print_summary(plan)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
