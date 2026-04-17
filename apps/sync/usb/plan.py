"""Dry-run CLI for USB mirror plans.

Usage::

    python -m apps.sync.usb.plan --profile usb-profiles/example.yaml
    python -m apps.sync.usb.plan --profile ... --json plan.json
    python -m apps.sync.usb.plan --profile ... --drive-root /Volumes/GIG-A

Writes nothing to the drive. Used for review before ``apply``.

Exit codes
----------

* 0 -- plan built OK.
* 2 -- profile load error.
* 3 -- preflight error (drive not mounted, label mismatch, etc.).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from apps.sync.usb import profile as profile_mod
from apps.sync.usb.diff import Plan, compute_plan, plan_summary
from apps.sync.usb.preflight import ALL_CHECKS, preflight
from apps.sync.usb.state import load_canonical_tracks

console = Console(width=120)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m apps.sync.usb.plan",
        description="Dry-run USB mirror plan (no writes).",
    )
    p.add_argument("--profile", required=True, help="Path to usb-profiles/<name>.yaml")
    p.add_argument(
        "--drive-root",
        default=None,
        help="Override drive mount path (default: /Volumes/<profile.drive_label>)",
    )
    p.add_argument(
        "--json",
        dest="json_out",
        default=None,
        help="Write the plan as JSON to this path (machine-readable).",
    )
    p.add_argument(
        "--skip-check",
        action="append",
        default=[],
        choices=list(ALL_CHECKS),
        help="Skip a named pre-flight check. May be given multiple times.",
    )
    p.add_argument(
        "--from-shared-state",
        action="store_true",
        help="Read canonical tracks from apps.shared.state (Phase 5) if available.",
    )
    p.add_argument(
        "--no-preflight",
        action="store_true",
        help="Skip preflight entirely (useful for local offline planning).",
    )
    return p


def _render_plan(plan: Plan) -> None:
    counts = plan_summary(plan)
    summary = Table(title=f"USB plan: {plan.profile_name}", show_lines=False)
    summary.add_column("kind")
    summary.add_column("count", justify="right")
    for key in ("copy", "transcode", "overwrite", "delete", "skip"):
        summary.add_row(key, str(counts.get(key, 0)))
    console.print(summary)

    console.print(
        f"[bold]total bytes[/bold]={plan.total_bytes:,}  "
        f"[bold]existing[/bold]={plan.existing_bytes:,}  "
        f"[bold]free needed[/bold]={plan.free_bytes_needed:,}"
    )

    if plan.warnings:
        console.print("[yellow]warnings:[/yellow]")
        for w in plan.warnings:
            console.print(f"  - {w}")

    if plan.ops:
        table = Table(title="Ops (first 30)", show_lines=False)
        table.add_column("kind")
        table.add_column("dst_rel")
        table.add_column("bytes", justify="right")
        table.add_column("reason")
        for op in plan.ops[:30]:
            table.add_row(op.kind, str(op.dst_rel), f"{op.bytes_estimate:,}", op.reason)
        console.print(table)
        if len(plan.ops) > 30:
            console.print(f"... ({len(plan.ops) - 30} more ops not shown)")


def plan_to_jsonable(plan: Plan) -> dict:
    return {
        "profile_name": plan.profile_name,
        "drive_root": str(plan.drive_root),
        "total_bytes": plan.total_bytes,
        "existing_bytes": plan.existing_bytes,
        "free_bytes_needed": plan.free_bytes_needed,
        "warnings": plan.warnings,
        "ops": [
            {
                "kind": op.kind,
                "dst": str(op.dst),
                "dst_rel": str(op.dst_rel),
                "src": str(op.src) if op.src else None,
                "stable_id": op.stable_id,
                "expected_hash": op.expected_hash,
                "reason": op.reason,
                "bytes_estimate": op.bytes_estimate,
            }
            for op in plan.ops
        ],
        "counts": plan_summary(plan),
    }


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        profile = profile_mod.load(args.profile)
    except FileNotFoundError as exc:
        console.print(f"[red]profile not found: {exc}[/red]")
        return 2
    except profile_mod.ProfileError as exc:
        console.print(f"[red]profile error: {exc}[/red]")
        return 2

    drive_root = Path(args.drive_root) if args.drive_root else profile.mount_point

    try:
        canonical = load_canonical_tracks(
            playlist_names=profile.playlists,
            use_shared_state=args.from_shared_state,
        )
    except Exception as exc:  # pragma: no cover (defensive)
        console.print(f"[red]state load error: {type(exc).__name__}: {exc}[/red]")
        return 2

    plan = compute_plan(
        profile=profile,
        canonical=canonical,
        drive_root=drive_root,
    )

    if not args.no_preflight:
        pre = preflight(
            profile,
            plan,
            drive_root=drive_root,
            write_probe=False,  # plan never writes
            skip_checks=set(args.skip_check),
        )
        if not pre.ok:
            console.print("[red]preflight errors:[/red]")
            for err in pre.errors:
                console.print(f"  - {err}")
            _render_plan(plan)
            return 3
        for w in pre.warnings:
            console.print(f"[yellow]preflight warn:[/yellow] {w}")

    _render_plan(plan)

    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(
            json.dumps(plan_to_jsonable(plan), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        console.print(f"[green]wrote {args.json_out}[/green]")

    return 0


if __name__ == "__main__":
    sys.exit(main())
