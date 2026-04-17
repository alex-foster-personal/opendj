"""Apply a USB mirror plan (cautious or bulk).

Usage::

    # No mode flag -> refuses and exits 3.
    python -m apps.sync.usb.apply --profile usb-profiles/example.yaml

    # Cautious: subset of playlists.
    python -m apps.sync.usb.apply --profile ... --cautious \\
        --playlists "Warmup"

    # Cautious: explicit files.
    python -m apps.sync.usb.apply --profile ... --cautious \\
        --files "Artist/Album/Track.mp3"

    # Bulk: full mirror.
    python -m apps.sync.usb.apply --profile ... --i-understand-the-risks

    # Remediate drift (Plan 10-02 extension).
    python -m apps.sync.usb.apply --profile ... --remediate-drift --cautious

Exit codes
----------

* 0 -- every op succeeded.
* 2 -- profile load error.
* 3 -- refused (no mode flag) or preflight failure.
* 4 -- one or more ops failed.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, TextColumn, BarColumn, TimeElapsedColumn
from rich.table import Table

from apps.shared import paths as _paths
from apps.sync.usb import profile as profile_mod
from apps.sync.usb.copy import (
    CopyResult,
    copy_one,
    delete_one,
    rename_one,
    transcode_one,
)
from apps.sync.usb.diff import (
    Plan,
    compute_plan,
    filter_plan_by_files,
    filter_plan_by_playlists,
    plan_summary,
)
from apps.sync.usb.marker import read_marker, resolve_drive_uuid, write_marker
from apps.sync.usb.playlist_writer import write_m3u8s
from apps.sync.usb.preflight import ALL_CHECKS, preflight
from apps.sync.usb.reversal import ReversalLog, verify_syntax
from apps.sync.usb.state import group_by_playlist, load_canonical_tracks

console = Console(width=120)

REVERSAL_DIR: Path = _paths.DATA_DIR / "usb"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m apps.sync.usb.apply",
        description="Apply a USB mirror plan (cautious / bulk).",
    )
    p.add_argument("--profile", required=True, help="Path to profile YAML")
    p.add_argument(
        "--drive-root",
        default=None,
        help="Override mount path (default: /Volumes/<drive_label>)",
    )

    mode = p.add_argument_group("mode (one required)")
    mode.add_argument(
        "--cautious",
        action="store_true",
        help="Restrict to --playlists or --files subset.",
    )
    mode.add_argument(
        "--i-understand-the-risks",
        dest="bulk",
        action="store_true",
        help="Apply the full plan (bulk).",
    )

    p.add_argument("--playlists", default=None, help="Comma-separated playlist names")
    p.add_argument("--files", default=None, help="Comma-separated drive-relative dst paths")
    p.add_argument("--reason", default="", help="Stored in the reversal script header.")
    p.add_argument(
        "--skip-check",
        action="append",
        default=[],
        choices=list(ALL_CHECKS),
        help="Skip a named pre-flight check.",
    )
    p.add_argument(
        "--from-shared-state",
        action="store_true",
        help="Read canonical tracks from apps.shared.state if available.",
    )
    p.add_argument(
        "--remediate-drift",
        action="store_true",
        help="Reconcile drift reported by verify (Plan 10-02).",
    )
    p.add_argument(
        "--only-corrupted",
        action="store_true",
        help="With --remediate-drift, limit to CORRUPTED files.",
    )
    p.add_argument(
        "--only-missing",
        action="store_true",
        help="With --remediate-drift, limit to MISSING files.",
    )
    p.add_argument(
        "--ffmpeg-path",
        default=None,
        help="Override ffmpeg executable (useful for tests).",
    )
    return p


def _execute_op(op, profile, *, ffmpeg: str | None) -> CopyResult:
    if op.kind == "copy":
        return copy_one(op, profile)
    if op.kind == "transcode":
        return transcode_one(op, profile, ffmpeg=ffmpeg or "ffmpeg")
    if op.kind == "overwrite":
        return copy_one(op, profile)
    if op.kind == "delete":
        return delete_one(op)
    if op.kind == "rename":
        # Rename ops are only emitted by Plan 10-02's remediation engine;
        # the source path is carried on ``op.src``.
        assert op.src is not None, "rename op must carry src"
        return rename_one(op, from_path=op.src)
    if op.kind == "skip":
        return CopyResult(op=op, ok=True, actual_hash="", dst_size=0, error=None)
    raise ValueError(f"unknown op kind: {op.kind}")


def _run_plan(
    *,
    plan: Plan,
    profile,
    mode: str,
    reason: str,
    drive_uuid: str | None,
    ffmpeg: str | None,
    tag: str = "apply",
) -> tuple[int, int, ReversalLog]:
    """Execute every op in ``plan``; return (ok_count, fail_count, reversal)."""
    reversal = ReversalLog.open(
        dir_=REVERSAL_DIR,
        profile_name=plan.profile_name,
        mode=mode,
        reason=reason,
        drive_uuid=drive_uuid,
        tag=tag,
    )
    ok = 0
    fail = 0
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        transient=True,
    ) as bar:
        t = bar.add_task("applying", total=len(plan.ops))
        for op in plan.ops:
            result = _execute_op(op, profile, ffmpeg=ffmpeg)
            reversal.append(result)
            if result.ok:
                ok += 1
            else:
                fail += 1
                console.print(
                    f"[red]FAILED[/red] {op.kind} {op.dst_rel}: {result.error}"
                )
            bar.advance(t)
    reversal.close(summary=f"{ok} ok / {fail} failed")
    return ok, fail, reversal


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    # --- mode resolution ----------------------------------------------------
    if not (args.cautious or args.bulk):
        console.print(
            "[red]refusing:[/red] pass --cautious or --i-understand-the-risks"
        )
        return 3
    if args.cautious and args.bulk:
        console.print("[red]--cautious and --i-understand-the-risks are mutually exclusive[/red]")
        return 3

    mode = "bulk" if args.bulk else "cautious"

    # --- profile + canonical ------------------------------------------------
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
        console.print(f"[red]state load error: {exc}[/red]")
        return 2

    plan = compute_plan(
        profile=profile,
        canonical=canonical,
        drive_root=drive_root,
    )

    # --- drift remediation (Plan 10-02) -------------------------------------
    if args.remediate_drift:
        from apps.sync.usb.verify import (
            FileStatus,
            plan_from_verify,
            verify_drive,
        )

        report = verify_drive(
            profile=profile,
            canonical=canonical,
            drive_root=drive_root,
        )
        statuses: set[FileStatus] | None = None
        if args.only_corrupted:
            statuses = {FileStatus.CORRUPTED}
        elif args.only_missing:
            statuses = {FileStatus.MISSING}
        plan = plan_from_verify(
            profile=profile,
            canonical=canonical,
            report=report,
            drive_root=drive_root,
            restrict_statuses=statuses,
        )

    # --- mode filtering -----------------------------------------------------
    if args.cautious:
        if args.playlists:
            names = [s.strip() for s in args.playlists.split(",") if s.strip()]
            plan = filter_plan_by_playlists(plan, names)
        elif args.files:
            files = [s.strip() for s in args.files.split(",") if s.strip()]
            plan = filter_plan_by_files(plan, files)
        else:
            console.print(
                "[red]--cautious requires --playlists or --files[/red]"
            )
            return 3

    if not plan.ops:
        console.print("[green]nothing to do (0 ops).[/green]")
        return 0

    # --- preflight ----------------------------------------------------------
    existing_marker = read_marker(drive_root) if drive_root.exists() else None
    drive_uuid = resolve_drive_uuid(drive_root, existing_marker=existing_marker)
    pre = preflight(
        profile,
        plan,
        drive_root=drive_root,
        write_probe=True,
        skip_checks=set(args.skip_check),
        marker_uuid=drive_uuid,
        ffmpeg_path=args.ffmpeg_path,
    )
    if not pre.ok:
        console.print("[red]preflight errors:[/red]")
        for err in pre.errors:
            console.print(f"  - {err}")
        return 3
    for w in pre.warnings:
        console.print(f"[yellow]preflight warn:[/yellow] {w}")

    # --- execute ------------------------------------------------------------
    console.print(
        f"[bold]apply[/bold] mode={mode} profile={profile.name} "
        f"drive={drive_root} ops={len(plan.ops)}"
    )
    ok, fail, reversal = _run_plan(
        plan=plan,
        profile=profile,
        mode=mode,
        reason=args.reason,
        drive_uuid=drive_uuid,
        ffmpeg=args.ffmpeg_path,
        tag="remediate-drift" if args.remediate_drift else "apply",
    )

    # --- m3u8 emission ------------------------------------------------------
    tracks_by_playlist = group_by_playlist(canonical)
    try:
        written = write_m3u8s(
            profile=profile,
            tracks_by_playlist=tracks_by_playlist,
            drive_root=drive_root,
        )
        for p in written:
            console.print(f"[green]wrote[/green] {p.relative_to(drive_root)}")
    except OSError as exc:
        console.print(f"[yellow]m3u8 emission failed: {exc}[/yellow]")

    # --- marker -------------------------------------------------------------
    try:
        marker = write_marker(
            drive_root,
            profile_name=profile.name,
            drive_uuid=drive_uuid,
            reversal_log=reversal.path,
            plan_summary=plan_summary(plan),
        )
        console.print(f"marker -> {marker}")
    except OSError as exc:
        console.print(f"[yellow]marker write failed: {exc}[/yellow]")

    # --- summary ------------------------------------------------------------
    table = Table(title="USB apply summary")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("ops succeeded", str(ok))
    table.add_row("ops failed", str(fail))
    table.add_row("reversal log", str(reversal.path))
    console.print(table)
    ok_syntax, err = verify_syntax(reversal.path)
    if not ok_syntax:
        console.print(f"[yellow]reversal syntax warn: {err}[/yellow]")
    return 0 if fail == 0 else 4


if __name__ == "__main__":
    sys.exit(main())
