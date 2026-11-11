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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.table import Table

from apps.sync.usb import profile as profile_mod
from apps.sync.usb.diff import Op, Plan, compute_plan, plan_summary
from apps.sync.usb.layout import dst_relpath
from apps.sync.usb.preflight import ALL_CHECKS, preflight
from apps.sync.usb.state import CanonicalTrack, load_canonical_tracks

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
        help="Drive mount path (defaults from the profile label on macOS; required elsewhere)",
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


# ------------------------------------------------- parallel playlist copy


@dataclass(slots=True, frozen=True)
class StickJob:
    """One independent copy job: the ops that land on a single stick."""

    stick: str
    root: Path
    ops: tuple[Op, ...]

    @property
    def total_bytes(self) -> int:
        return sum(op.bytes_estimate for op in self.ops)


@dataclass(slots=True, frozen=True)
class ParallelCopyPlan:
    """One playlist split across several sticks, one :class:`StickJob` per stick."""

    playlist: str
    jobs: tuple[StickJob, ...]
    warnings: tuple[str, ...] = ()
    omitted: tuple[str, ...] = ()

    @property
    def sticks(self) -> tuple[str, ...]:
        return tuple(job.stick for job in self.jobs)

    def assignments(self) -> dict[str, str]:
        """``stable_id -> stick`` for every planned track."""
        return {op.stable_id: job.stick for job in self.jobs for op in job.ops if op.stable_id}


def plan_parallel_playlist_copy(
    *,
    playlist: str,
    tracks: Sequence[CanonicalTrack],
    sticks: Mapping[str, Path],
    layout: str,
    format_: str = "copy-as-is",
) -> ParallelCopyPlan:
    """Partition ``playlist``'s tracks across ``sticks`` without touching any disk.

    Each selected track goes to exactly one stick (a repeated ``stable_id`` is
    planned once). Tracks are placed largest first on the stick holding the
    fewest bytes so far, so the jobs finish close together. Every stick gets a
    job, even an empty one, so the joined plan names every target. Only
    ``copy-as-is`` is supported: the joined verification compares the copied
    bytes with the library hash, which a transcode would never match.

    Two tracks never share a destination on one stick: paths are compared
    case-insensitively (FAT/exFAT sticks fold case), a track whose path is
    already taken goes to the least-loaded stick where it is free, and a track
    whose path is taken on every stick is left out with a warning (and listed
    in ``omitted``, which fails the joined receipt) rather than overwriting
    another track.
    """
    if not sticks:
        raise ValueError("parallel copy needs at least one stick")
    if format_ != "copy-as-is":
        raise ValueError(f"parallel copy supports copy-as-is only, not {format_!r}")

    selected: list[tuple[int, CanonicalTrack]] = []
    seen: set[str] = set()
    for track in tracks:
        if track.playlist != playlist or track.stable_id in seen:
            continue
        seen.add(track.stable_id)
        selected.append((len(selected) + 1, track))

    names = list(sticks)
    loads = {name: 0 for name in names}
    placed: dict[str, list[Op]] = {name: [] for name in names}
    taken: dict[str, dict[str, str]] = {name: {} for name in names}
    warnings: list[str] = []
    omitted: list[str] = []
    for index, track in sorted(selected, key=lambda it: (-it[1].size_bytes, it[1].stable_id)):
        rel = dst_relpath(
            layout=layout,
            format_=format_,
            playlist=playlist,
            track_index=index,
            artist=track.artist,
            album=track.album,
            title=track.title,
            src=track.source_path,
        )
        key = rel.as_posix().casefold()
        free = [n for n in names if key not in taken[n]]
        if not free:
            warnings.append(
                f"destination collision: {rel.as_posix()} ({track.stable_id}) is taken on every stick; not planned"
            )
            omitted.append(track.stable_id)
            continue
        target = min(free, key=lambda n: (loads[n], len(placed[n]), names.index(n)))
        taken[target][key] = track.stable_id
        placed[target].append(
            Op(
                kind="copy",
                dst=Path(sticks[target]) / rel,
                dst_rel=rel,
                reason=f"parallel copy of {playlist!r} to {target}",
                bytes_estimate=track.size_bytes,
                src=track.source_path,
                stable_id=track.stable_id,
                expected_hash=track.content_hash,
            )
        )
        loads[target] += track.size_bytes

    jobs = tuple(StickJob(stick=n, root=Path(sticks[n]), ops=tuple(placed[n])) for n in names)
    return ParallelCopyPlan(playlist=playlist, jobs=jobs, warnings=tuple(warnings), omitted=tuple(omitted))


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        profile = profile_mod.load(args.profile)
        drive_root = Path(args.drive_root) if args.drive_root else profile.mount_point
    except FileNotFoundError as exc:
        console.print(f"[red]profile not found: {exc}[/red]")
        return 2
    except profile_mod.ProfileError as exc:
        console.print(f"[red]profile error: {exc}[/red]")
        return 2

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
