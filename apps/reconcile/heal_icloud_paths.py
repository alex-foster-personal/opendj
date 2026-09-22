"""Heal Rekordbox FolderPaths that sit in the iCloud zone toward MUSIC_ROOTS.

Entry points::

    # Plan only (default): scan zone paths, rank Music twins, write JSON.
    python -m apps.reconcile.heal_icloud_paths

    # Apply unique basename+identity Music twins (live RB write; confirm).
    python -m apps.reconcile.heal_icloud_paths --apply-unique --i-understand-the-risks

Ambiguous / fuzzy-only rows stay ``needs_confirm`` for relocate / apply --tracks.
Never mechanical Documents->Music prefix rewrite. Never brctl download.

TODO(machine-paths): MUSIC_ROOTS and iCloud-zone roots are host-local. Other
machines must set MDT_MUSIC_ROOTS / path-map; do not assume this laptop's
/Users/<name>/Music layout. See apps.shared.icloud_zone module docstring.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from rich.console import Console
from rich.table import Table

from apps.reconcile import apply as reconcile_apply
from apps.reconcile import locate
from apps.shared import audio_files, fs_residency, icloud_zone, paths, rekordbox_db

console = Console(width=120)

OUT_JSON: Path = paths.DATA_DIR / "reconcile" / "icloud-heal-plan.json"

Status = Literal["ready_unique", "needs_confirm", "no_twin", "already_music"]

_IDENTITY_SIGNALS = frozenset({"size_match", "id3_match", "duration_match"})

MACHINE_PATHS_WARNING = (
    "TODO(machine-paths): MUSIC_ROOTS and iCloud-zone roots are host-local. "
    "Other hosts must set MDT_MUSIC_ROOTS and MDT_PATH_MAP / data/path-map.json. "
    "heal-to-Music is not a cross-machine path-map substitute. Apply RB rewrites "
    "only on the host that owns that Rekordbox library."
)


@dataclass(slots=True)
class HealRow:
    id: str
    title: str
    artist: str
    original_path: str
    zone_reason: str
    status: Status
    candidate_path: str = ""
    confidence: float = 0.0
    signals: list[str] = field(default_factory=list)
    rationale: str = ""
    candidates_total: int = 0


def _row_dict(track: rekordbox_db.RBTrack) -> dict[str, str]:
    original = track.folder_path or ""
    return {
        "id": track.id,
        "title": track.title or "",
        "artist": track.artist or "",
        "duration_s": "" if track.duration_s is None else str(int(track.duration_s)),
        "file_size": "" if track.file_size is None else str(track.file_size),
        "original_path": original,
        "basename": Path(original).name if original else "",
    }


def _meets_unique_bar(cand: locate.Candidate) -> bool:
    sigs = set(cand.signals)
    return "basename_exact" in sigs and bool(sigs & _IDENTITY_SIGNALS)


def _classify(
    track: rekordbox_db.RBTrack,
    candidates: list[locate.Candidate],
) -> HealRow:
    original = track.folder_path or ""
    reason = icloud_zone.icloud_zone_reason(original) or "unknown"
    base = HealRow(
        id=track.id,
        title=track.title or "",
        artist=track.artist or "",
        original_path=original,
        zone_reason=reason,
        status="no_twin",
        candidates_total=len(candidates),
    )
    if not candidates:
        base.rationale = "no materialised Music twin under MUSIC_ROOTS"
        return base

    unique = [c for c in candidates if _meets_unique_bar(c)]
    if len(unique) == 1:
        best = unique[0]
        base.status = "ready_unique"
        base.candidate_path = str(best.path)
        base.confidence = best.confidence
        base.signals = list(best.signals)
        base.rationale = (
            f"unique Music twin; signals={','.join(best.signals)}"
        )
        return base

    best = candidates[0]
    base.status = "needs_confirm"
    base.candidate_path = str(best.path)
    base.confidence = best.confidence
    base.signals = list(best.signals)
    if len(unique) > 1:
        base.rationale = f"{len(unique)} unique-bar Music twins; confirm required"
    else:
        base.rationale = (
            "best candidate lacks basename_exact+identity; confirm required"
        )
    return base


def collect_zone_tracks(db: Any) -> list[rekordbox_db.RBTrack]:
    """RB tracks whose FolderPath is in the iCloud zone (streaming excluded)."""
    out: list[rekordbox_db.RBTrack] = []
    for track in rekordbox_db.iter_tracks(db):
        folder = track.folder_path or ""
        if not folder or track.is_streaming:
            continue
        # Streaming URIs and relative junk are not filesystem heal targets.
        if not Path(folder).is_absolute():
            continue
        if rekordbox_db.is_streaming_path(folder):
            continue
        if icloud_zone.is_icloud_zone(folder):
            out.append(track)
    return out

def build_plan(
    tracks: list[rekordbox_db.RBTrack] | None = None,
) -> list[HealRow]:
    """Score Music twins for each iCloud-zone FolderPath."""
    icloud_zone.assert_music_roots_outside_icloud_zone()
    if tracks is None:
        paths.copy_live_dbs()
        db = rekordbox_db.open_db()
        tracks = collect_zone_tracks(db)

    index = locate.FsIndex.build(audio_files.scan_music_files())
    id3_cache: dict[Path, audio_files.AudioMetadata | None] = {}
    rows: list[HealRow] = []
    for track in tracks:
        # Full candidate set (not truncated) so uniqueness is honest.
        cands = locate._locate_candidates(_row_dict(track), index, id3_cache)
        rows.append(_classify(track, cands))
    return rows


def write_plan(rows: list[HealRow], out: Path = OUT_JSON) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "warnings": [MACHINE_PATHS_WARNING],
        "music_roots": [str(r) for r in paths.MUSIC_ROOTS],
        "counts": {
            "total": len(rows),
            "ready_unique": sum(1 for r in rows if r.status == "ready_unique"),
            "needs_confirm": sum(1 for r in rows if r.status == "needs_confirm"),
            "no_twin": sum(1 for r in rows if r.status == "no_twin"),
        },
        "rows": [asdict(r) for r in rows],
    }
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return out


def _print_summary(rows: list[HealRow]) -> None:
    counts = {
        "ready_unique": sum(1 for r in rows if r.status == "ready_unique"),
        "needs_confirm": sum(1 for r in rows if r.status == "needs_confirm"),
        "no_twin": sum(1 for r in rows if r.status == "no_twin"),
    }
    table = Table(title="iCloud path heal plan", show_lines=False)
    table.add_column("Status", style="bold")
    table.add_column("Count", justify="right")
    for key, n in counts.items():
        colour = {"ready_unique": "green", "needs_confirm": "yellow", "no_twin": "red"}[key]
        table.add_row(key, f"[{colour}]{n}[/{colour}]")
    table.add_row("total zone paths", str(len(rows)))
    console.print(table)
    console.print(f"[dim]{MACHINE_PATHS_WARNING}[/dim]")


def _build_heal_updates(unique: list[HealRow]) -> tuple[list[reconcile_apply.Update], int]:
    updates: list[reconcile_apply.Update] = []
    for r in unique:
        twin = Path(r.candidate_path)
        if icloud_zone.is_icloud_zone(twin):
            console.print(f"[red]Refuse zone candidate {twin}[/red]")
            return [], 2
        if not fs_residency.is_materialised(twin):
            console.print(f"[red]Refuse non-materialised candidate {twin}[/red]")
            return [], 2
        updates.append(
            reconcile_apply.Update(
                id=r.id,
                title=r.title,
                artist=r.artist,
                old_path=r.original_path,
                new_path=str(twin.resolve()),
                confidence=r.confidence,
                rationale=r.rationale,
                triple_validated=True,
            )
        )
    return updates, 0


def _commit_heal_updates(updates: list[reconcile_apply.Update]) -> int:
    reconcile_apply._print_preview(updates, "iCloud heal --apply-unique")
    if reconcile_apply._rekordbox_running():
        console.print("[red]Rekordbox is running; quit it first.[/red]")
        return 2
    if not reconcile_apply._confirm():
        console.print("[yellow]Confirmation failed; aborting.[/yellow]")
        return 2

    backup = reconcile_apply._backup_db(reconcile_apply.DEFAULT_BACKUP_DIR)
    errors = reconcile_apply._apply_updates(updates)
    if errors:
        console.print(f"[red]{len(errors)} write error(s); check DB / restore {backup}[/red]")
        for u, err in errors:
            console.print(f"  {u.id}: {err}")
        return 1
    failures = reconcile_apply._verify(updates)
    if failures:
        console.print(f"[red]{len(failures)} verify failure(s); restore {backup}[/red]")
        for u, err in failures:
            console.print(f"  {u.id}: {err}")
        return 1
    console.print(
        f"[green]Applied {len(updates)} FolderPath heal(s). Backup: {backup}[/green]"
    )
    return 0


def apply_unique(
    rows: list[HealRow],
    *,
    i_understand: bool,
) -> int:
    """Live-write FolderPath for ready_unique rows via reconcile.apply rails."""
    unique = [r for r in rows if r.status == "ready_unique" and r.candidate_path]
    if not unique:
        console.print("[yellow]No ready_unique rows to apply.[/yellow]")
        return 0
    if not i_understand:
        console.print(
            "[red]Refusing apply without --i-understand-the-risks.[/red]"
        )
        return 2

    updates, code = _build_heal_updates(unique)
    if code:
        return code
    return _commit_heal_updates(updates)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m apps.reconcile.heal_icloud_paths",
        description=(
            "Plan (default) or apply-unique Music twins for iCloud-zone FolderPaths. "
            + MACHINE_PATHS_WARNING
        ),
    )
    p.add_argument(
        "--apply-unique",
        action="store_true",
        help="live-write FolderPath for ready_unique rows only (requires confirm)",
    )
    p.add_argument(
        "--i-understand-the-risks",
        action="store_true",
        help="required with --apply-unique",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=OUT_JSON,
        help=f"plan JSON path (default {OUT_JSON})",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    console.print("[bold]iCloud path heal[/bold]")
    console.print(f"  MUSIC_ROOTS: {', '.join(str(r) for r in paths.MUSIC_ROOTS)}")
    rows = build_plan()
    out = write_plan(rows, args.out)
    _print_summary(rows)
    console.print(f"Plan written: {out}")
    if args.apply_unique:
        return apply_unique(rows, i_understand=args.i_understand_the_risks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
