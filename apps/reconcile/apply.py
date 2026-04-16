"""Apply located path updates to Rekordbox — dry-run by default, safety-gated live.

Entry points::

    # Dry-run: prints preview + writes data/reconcile/patch.json (safe).
    python -m apps.reconcile.apply --dry-run

    # Live: writes to the live master.db. Requires all three flags plus a
    # typed "yes I understand" prompt. Max 3 IDs per invocation.
    python -m apps.reconcile.apply --live --i-understand-the-risks \\
        --tracks 12345,67890

Safety rails for ``--live`` (all must pass or we abort):
  1. Typed confirmation prompt ("yes I understand").
  2. Refuse to run if Rekordbox is open (pgrep -if rekordbox).
  3. Timestamped backup of master.db before any write.
  4. Open the live DB, update FolderPath, commit, close.
  5. Reopen the live DB, verify FolderPath reads back + file exists on disk.
  6. Emit a stand-alone ``reverse-{ts}.py`` script that restores the old
     FolderPath values.
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.table import Table

from apps.shared import paths
from apps.reconcile import locate as _locate

console = Console(width=120)

DEFAULT_INPUT: Path = _locate.OUT_CSV
DEFAULT_PATCH: Path = paths.DATA_DIR / "reconcile" / "patch.json"
DEFAULT_BACKUP_DIR: Path = paths.DATA_DIR / "reconcile" / "backups"

CONFIRMATION_PHRASE = "yes i understand"
MAX_LIVE_TRACKS = 3


@dataclass(slots=True)
class Update:
    """One pending ``FolderPath`` update derived from ``located.csv``."""

    id: str
    title: str
    artist: str
    old_path: str
    new_path: str
    confidence: float
    rationale: str
    triple_validated: bool


# ------------------------------------------------------------------ input


def _load_updates(csv_path: Path) -> list[Update]:
    if not csv_path.exists():
        console.print(f"[red]Missing {csv_path}. Run `locate` first.[/red]")
        raise SystemExit(1)
    updates: list[Update] = []
    with csv_path.open("r", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            new_path = row.get("best_candidate_path") or ""
            if not new_path:
                continue
            updates.append(
                Update(
                    id=row["id"],
                    title=row.get("title", ""),
                    artist=row.get("artist", ""),
                    old_path=row.get("original_path", ""),
                    new_path=new_path,
                    confidence=float(row.get("confidence", "0") or 0),
                    rationale=row.get("rationale", ""),
                    triple_validated=(row.get("triple_validated") == "True"),
                )
            )
    return updates


def _filter_updates(
    updates: list[Update],
    track_ids: list[str] | None,
) -> list[Update]:
    """Apply the --tracks filter if given; otherwise keep only validated."""
    if track_ids:
        by_id = {u.id: u for u in updates}
        missing = [tid for tid in track_ids if tid not in by_id]
        if missing:
            console.print(
                f"[yellow]Warning:[/yellow] requested IDs not in located.csv: "
                f"{', '.join(missing)}"
            )
        chosen = [by_id[tid] for tid in track_ids if tid in by_id]
        for u in chosen:
            if not u.triple_validated:
                console.print(
                    f"[yellow]Warning:[/yellow] ID {u.id} is NOT triple-validated"
                    f" (confidence {u.confidence:.3f}) — included anyway."
                )
        return chosen
    return [u for u in updates if u.triple_validated]


# ------------------------------------------------------------------ preview


def _print_preview(updates: list[Update], banner: str) -> None:
    console.print(f"[bold cyan]{banner}[/bold cyan]")
    table = Table(title=f"{len(updates)} pending update(s)", show_lines=True)
    table.add_column("ID", style="bold")
    table.add_column("Title")
    table.add_column("Artist")
    table.add_column("Old → New")
    table.add_column("Conf", justify="right")
    for u in updates:
        arrow = f"[dim]{u.old_path}[/dim]\n→ {u.new_path}"
        table.add_row(u.id, u.title, u.artist, arrow, f"{u.confidence:.3f}")
    console.print(table)


def _write_patch(updates: list[Update], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {
            "id": u.id,
            "old_path": u.old_path,
            "new_path": u.new_path,
            "confidence": u.confidence,
            "rationale": u.rationale,
        }
        for u in updates
    ]
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------ safety


def _rekordbox_running() -> bool:
    """True if any process matches ``rekordbox`` via pgrep -if."""
    try:
        r = subprocess.run(
            ["pgrep", "-if", "rekordbox"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        console.print("[yellow]pgrep not available — cannot verify RB is closed.[/yellow]")
        return False
    return r.returncode == 0 and bool(r.stdout.strip())


def _confirm() -> bool:
    console.print(
        "[bold red]LIVE MODE.[/bold red] This will modify the live "
        f"{paths.REKORDBOX_LIVE_DB}."
    )
    console.print(f"Type exactly [bold]{CONFIRMATION_PHRASE}[/bold] to proceed:")
    try:
        answer = input("> ").strip().lower()
    except EOFError:
        return False
    return answer == CONFIRMATION_PHRASE


def _backup_db(backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    dst = backup_dir / f"master.{ts}.db"
    shutil.copy2(paths.REKORDBOX_LIVE_DB, dst)
    size = dst.stat().st_size
    if size <= 0:
        raise RuntimeError(f"Backup failed: {dst} has size {size}")
    console.print(f"[green]Backup OK[/green] → {dst} ({size:,} bytes)")
    return dst


# ------------------------------------------------------------------ write


def _open_live_db():
    """Import + open the live RB DB. Imported lazily to keep dry-run cheap."""
    from pyrekordbox import Rekordbox6Database  # noqa: PLC0415

    return Rekordbox6Database(path=str(paths.REKORDBOX_LIVE_DB))


def _apply_updates(updates: list[Update]) -> list[tuple[Update, str]]:
    """Write each update to the live DB. Returns (update, error_or_empty)."""
    db = _open_live_db()
    errors: list[tuple[Update, str]] = []
    try:
        for u in updates:
            try:
                row = db.get_content(ID=u.id).one_or_none()
            except Exception as exc:  # noqa: BLE001
                errors.append((u, f"lookup failed: {exc}"))
                continue
            if row is None:
                errors.append((u, "ID not found in live DB"))
                continue
            row.FolderPath = u.new_path
        db.commit()
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass
    return errors


def _verify(updates: list[Update]) -> list[tuple[Update, str]]:
    """Reopen the live DB and verify each FolderPath reads back + exists."""
    db = _open_live_db()
    failures: list[tuple[Update, str]] = []
    try:
        for u in updates:
            row = db.get_content(ID=u.id).one_or_none()
            if row is None:
                failures.append((u, "ID missing after write"))
                continue
            if row.FolderPath != u.new_path:
                failures.append(
                    (u, f"FolderPath mismatch after write: {row.FolderPath!r}")
                )
                continue
            if not Path(u.new_path).exists():
                failures.append((u, f"New path does not exist on disk: {u.new_path}"))
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass
    return failures


def _write_reversal_script(
    updates: list[Update],
    backup: Path,
    backup_dir: Path,
    ts: str,
) -> Path:
    out = backup_dir / f"reverse-{ts}.py"
    # Build a tiny self-contained script: no imports from this project, so it
    # still runs if the repo is moved/deleted.
    originals = [{"id": u.id, "old_path": u.old_path} for u in updates]
    body = f'''#!/usr/bin/env python3
"""Auto-generated reversal for reconcile apply run {ts}.

Restores the original FolderPath values for the IDs below. If this script
itself fails, you can always fall back to:

    cp "{backup}" "{paths.REKORDBOX_LIVE_DB}"
"""
from __future__ import annotations

from pyrekordbox import Rekordbox6Database

LIVE_DB = r"{paths.REKORDBOX_LIVE_DB}"
ORIGINALS = {json.dumps(originals, indent=4)}


def main() -> None:
    db = Rekordbox6Database(path=LIVE_DB)
    try:
        for entry in ORIGINALS:
            row = db.get_content(ID=entry["id"]).one_or_none()
            if row is None:
                print(f"  ! ID {{entry['id']}} not found; skipping")
                continue
            print(f"  • ID {{entry['id']}}: FolderPath -> {{entry['old_path']}}")
            row.FolderPath = entry["old_path"]
        db.commit()
    finally:
        db.close()
    print("Done.")


if __name__ == "__main__":
    main()
'''
    out.write_text(body)
    out.chmod(0o755)
    return out


# ------------------------------------------------------------------ flows


def _run_dry_run(updates: list[Update], patch_out: Path) -> int:
    _print_preview(updates, "DRY-RUN — no changes written to master.db")
    _write_patch(updates, patch_out)
    console.print(f"[green]Wrote patch → {patch_out}[/green]")
    return 0


def _run_live(updates: list[Update], backup_dir: Path) -> int:
    if not updates:
        console.print("[yellow]No updates to apply. Exiting.[/yellow]")
        return 1
    if len(updates) > MAX_LIVE_TRACKS:
        console.print(
            f"[red]Refusing:[/red] {len(updates)} updates exceeds "
            f"MAX_LIVE_TRACKS={MAX_LIVE_TRACKS}. A bulk mode does not yet "
            "exist — re-run with fewer --tracks or use --dry-run."
        )
        return 2

    _print_preview(updates, "LIVE — about to write to master.db")

    # Safety rail 2: is Rekordbox running?
    if _rekordbox_running():
        console.print(
            "[red]ABORT:[/red] Rekordbox is running. Quit Rekordbox completely "
            "(check the menu bar too) and retry. I will NOT auto-kill it."
        )
        return 3

    # Safety rail 1: typed confirmation.
    if not _confirm():
        console.print("[yellow]Confirmation not given. Aborting.[/yellow]")
        return 4

    # Safety rail 3: backup.
    ts = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    backup = _backup_db(backup_dir)

    # Safety rail 4: apply.
    console.print("[bold]Applying updates…[/bold]")
    errors = _apply_updates(updates)
    if errors:
        console.print("[red]Errors during apply:[/red]")
        for u, msg in errors:
            console.print(f"  ✗ {u.id}: {msg}")
        console.print(f"[red]Restore with:[/red] cp {backup} {paths.REKORDBOX_LIVE_DB}")
        return 5

    # Safety rail 5: verify.
    console.print("[bold]Verifying readback…[/bold]")
    failures = _verify(updates)
    if failures:
        console.print("[red]Verification failures:[/red]")
        for u, msg in failures:
            console.print(f"  ✗ {u.id}: {msg}")
        console.print(f"[red]Restore with:[/red] cp {backup} {paths.REKORDBOX_LIVE_DB}")
        return 6

    # Safety rail 6: reversal script.
    rev = _write_reversal_script(updates, backup, backup_dir, ts)
    console.print(f"[green]Reversal script → {rev}[/green]")
    console.print(f"[green]Full DB backup → {backup}[/green]")

    # Success table.
    ok = Table(title="Applied updates", show_lines=False)
    ok.add_column("ID", style="bold")
    ok.add_column("Title")
    ok.add_column("Before → After")
    for u in updates:
        ok.add_row(u.id, u.title, f"[dim]{u.old_path}[/dim] → {u.new_path}")
    console.print(ok)
    return 0


# ------------------------------------------------------------------ CLI


def _parse_ids(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [s.strip() for s in raw.split(",") if s.strip()]


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="apps.reconcile.apply",
        description="Apply located path updates to Rekordbox (dry-run or safety-gated live).",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="(default) preview + patch only")
    mode.add_argument("--live", action="store_true", help="write to the live master.db")
    p.add_argument(
        "--i-understand-the-risks",
        action="store_true",
        help="required companion flag for --live",
    )
    p.add_argument(
        "--tracks",
        type=str,
        default=None,
        help="comma-separated RB IDs. Required for --live; max 3. "
        "In --dry-run, omit to include all triple-validated rows.",
    )
    p.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="located.csv path")
    p.add_argument("--patch-out", type=Path, default=DEFAULT_PATCH, help="patch.json path")
    p.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR, help="backup dir")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # No flags? Show help.
    if not args.dry_run and not args.live:
        parser.print_help()
        return 0

    track_ids = _parse_ids(args.tracks)
    updates = _filter_updates(_load_updates(args.input), track_ids)

    if args.live:
        if not args.i_understand_the_risks:
            console.print(
                "[red]--live requires --i-understand-the-risks[/red]. Aborting."
            )
            return 2
        if not track_ids:
            console.print(
                "[red]--live requires --tracks[/red] (comma-separated, 1–3 IDs)."
            )
            return 2
        if len(track_ids) > MAX_LIVE_TRACKS:
            console.print(
                f"[red]Refusing:[/red] {len(track_ids)} IDs > MAX_LIVE_TRACKS"
                f"={MAX_LIVE_TRACKS}. No bulk mode yet — run a smaller batch or --dry-run."
            )
            return 2
        return _run_live(updates, args.backup_dir)

    # Dry run (default).
    return _run_dry_run(updates, args.patch_out)


if __name__ == "__main__":
    sys.exit(main())
