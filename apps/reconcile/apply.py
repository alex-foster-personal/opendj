"""Apply located path updates to Rekordbox — dry-run by default, safety-gated live.

Entry points::

    # Dry-run: prints preview + writes data/reconcile/patch.json (safe).
    python -m apps.reconcile.apply --dry-run

    # Live (cautious, 1–3 IDs): writes to the live master.db. Requires all
    # three flags plus a typed "yes I understand" prompt.
    python -m apps.reconcile.apply --live --i-understand-the-risks \\
        --tracks 12345,67890

    # Live (bulk): applies EVERY triple-validated row from located.csv that
    # is still broken in the current audit (data/rb_missing_files.csv).
    # Additional confirmation prompt requires typing "apply N updates".
    python -m apps.reconcile.apply --live --bulk --i-understand-the-risks

Safety rails for ``--live --tracks`` (all must pass or we abort):
  1. Typed confirmation prompt ("yes I understand").
  2. Refuse to run if Rekordbox is open (pgrep -if rekordbox).
  3. Timestamped backup of master.db before any write.
  4. Open the live DB, update FolderPath, commit, close.
  5. Reopen the live DB, verify FolderPath reads back + file exists on disk.
  6. Emit a stand-alone ``reverse-{ts}.py`` script that restores the old
     FolderPath values.

Extra rails for ``--live --bulk`` (on top of all six above):
  a. Intersects located.csv ∩ rb_missing_files.csv so we only touch rows
     that are currently broken.
  b. Pre-flight filesystem check: every new_path must exist on disk; any
     miss aborts BEFORE the backup is taken.
  c. Count-confirmation prompt: user must type ``apply N updates`` where N
     is the computed count (case-insensitive). A race check re-computes the
     count from disk right before commit; if it drifted, we abort.
  d. Single batched ``db.commit()`` — any per-row exception bubbles up and
     the whole transaction is discarded (session closed without commit).
  e. Progress output every 10 rows + final rich summary table
     (attempted / succeeded / failed).
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

from apps.reconcile import locate as _locate
from apps.shared import paths
from apps.shared.rekordbox_writeback import require_writeback_enabled

console = Console(width=120)

DEFAULT_INPUT: Path = _locate.OUT_CSV
DEFAULT_PATCH: Path = paths.DATA_DIR / "reconcile" / "patch.json"
# Manifest of the rows a SUCCESSFUL live/bulk apply actually wrote.
# patch.json is a dry-run preview and can describe an older run; the
# gated audit suite (tests/audit/test_ingest_pipeline.py) reads this
# file so it always audits the exact updates last applied for real.
DEFAULT_APPLIED: Path = paths.DATA_DIR / "reconcile" / "applied.json"
DEFAULT_BACKUP_DIR: Path = paths.DATA_DIR / "reconcile" / "backups"
DEFAULT_BROKEN_CSV: Path = paths.DATA_DIR / "rb_missing_files.csv"

CONFIRMATION_PHRASE = "yes i understand"
MAX_LIVE_TRACKS = 3
BULK_PROGRESS_EVERY = 10


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
    require_writeback_enabled("module.reconcile.apply")
    from pyrekordbox import Rekordbox6Database

    return Rekordbox6Database(path=str(paths.REKORDBOX_LIVE_DB))


def _apply_updates(updates: list[Update]) -> list[tuple[Update, str]]:
    """Write each update to the live DB. Returns (update, error_or_empty)."""
    db = _open_live_db()
    errors: list[tuple[Update, str]] = []
    try:
        for u in updates:
            try:
                row = db.get_content(ID=u.id)
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
            row = db.get_content(ID=u.id)
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

    cp "{backup.as_posix()}" "{paths.REKORDBOX_LIVE_DB.as_posix()}"
"""
from __future__ import annotations

from pyrekordbox import Rekordbox6Database

LIVE_DB = r"{paths.REKORDBOX_LIVE_DB}"
ORIGINALS = {json.dumps(originals, indent=4)}


def main() -> None:
    db = Rekordbox6Database(path=LIVE_DB)
    try:
        for entry in ORIGINALS:
            row = db.get_content(ID=entry["id"])
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


# ------------------------------------------------------------------ bulk


def _load_broken_ids(broken_csv: Path) -> set[str]:
    """Return the set of RB IDs that are currently broken per the audit."""
    if not broken_csv.exists():
        console.print(
            f"[red]Missing {broken_csv}[/red]. Run "
            "`python -m apps.audit.rekordbox_vs_music` first."
        )
        raise SystemExit(2)
    ids: set[str] = set()
    with broken_csv.open("r", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            tid = row.get("id")
            if tid:
                ids.add(tid)
    return ids


def _select_bulk_updates(
    all_updates: list[Update],
    broken_ids: set[str],
) -> list[Update]:
    """Triple-validated ∩ still-broken ∩ has-new-path."""
    return [
        u for u in all_updates
        if u.triple_validated
        and u.id in broken_ids
        and u.new_path
    ]


def _preflight_fs_check(updates: list[Update]) -> list[Update]:
    """Return updates whose new_path does NOT exist on disk (empty == OK)."""
    return [u for u in updates if not Path(u.new_path).exists()]


def _confirm_bulk_count(expected: int) -> bool:
    """Require user to type ``apply N updates`` (case-insensitive)."""
    phrase = f"apply {expected} updates"
    console.print(
        f"[bold red]BULK MODE.[/bold red] About to apply "
        f"[bold]{expected}[/bold] updates to the live "
        f"{paths.REKORDBOX_LIVE_DB}."
    )
    console.print(
        f"Type exactly [bold]{phrase}[/bold] to proceed "
        "(case-insensitive):"
    )
    try:
        answer = input("> ").strip().lower()
    except EOFError:
        return False
    return answer == phrase


def _apply_updates_strict(updates: list[Update]) -> list[tuple[Update, str]]:
    """Bulk apply: any per-row error aborts the whole batch without committing.

    Returns an empty list on success. On failure, returns the single
    (update, error) pair that triggered the abort — the caller should
    treat ANY non-empty return as "batch not committed".
    """
    db = _open_live_db()
    committed = False
    errors: list[tuple[Update, str]] = []
    try:
        for i, u in enumerate(updates, start=1):
            try:
                row = db.get_content(ID=u.id)
            except Exception as exc:  # noqa: BLE001
                errors.append((u, f"lookup failed: {exc}"))
                break
            if row is None:
                errors.append((u, "ID not found in live DB"))
                break
            try:
                row.FolderPath = u.new_path
            except Exception as exc:  # noqa: BLE001
                errors.append((u, f"assign failed: {exc}"))
                break
            if i % BULK_PROGRESS_EVERY == 0 or i == len(updates):
                console.print(
                    f"  [dim]staged {i}/{len(updates)}[/dim]"
                )
        if not errors:
            db.commit()
            committed = True
    finally:
        if not committed:
            # Best-effort rollback; pyrekordbox exposes the SA session.
            try:
                db.session.rollback()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass
    return errors


def _run_bulk(
    all_updates: list[Update],
    broken_csv: Path,
    backup_dir: Path,
) -> int:
    # Selection.
    broken_ids = _load_broken_ids(broken_csv)
    updates = _select_bulk_updates(all_updates, broken_ids)
    if not updates:
        console.print(
            "[yellow]No triple-validated rows intersect the current "
            f"{broken_csv.name}. Nothing to do.[/yellow]"
        )
        return 1
    expected_count = len(updates)

    _print_preview(updates, f"BULK LIVE — about to write {expected_count} rows")

    # Rail 2: is Rekordbox running?
    if _rekordbox_running():
        console.print(
            "[red]ABORT:[/red] Rekordbox is running. Quit Rekordbox "
            "completely (check the menu bar too) and retry. I will NOT "
            "auto-kill it."
        )
        return 3

    # Extra rail (b): pre-flight FS check BEFORE any backup or prompt.
    missing = _preflight_fs_check(updates)
    if missing:
        console.print(
            f"[red]ABORT:[/red] pre-flight filesystem check failed — "
            f"{len(missing)} candidate paths do not exist on disk:"
        )
        for u in missing[:10]:
            console.print(f"  ✗ {u.id}: {u.new_path}")
        if len(missing) > 10:
            console.print(f"  ... and {len(missing) - 10} more")
        return 7

    # Rail 1: typed "yes i understand".
    if not _confirm():
        console.print("[yellow]Confirmation not given. Aborting.[/yellow]")
        return 4

    # Extra rail (c): count-confirmation.
    if not _confirm_bulk_count(expected_count):
        console.print(
            "[yellow]Count confirmation mismatch. Aborting.[/yellow]"
        )
        return 4

    # Race check: re-derive the actionable set right before backup. If the
    # count moved between prompt and commit, bail loud.
    current_broken = _load_broken_ids(broken_csv)
    current = _select_bulk_updates(all_updates, current_broken)
    if len(current) != expected_count:
        console.print(
            f"[red]ABORT:[/red] actionable count drifted during prompt "
            f"({expected_count} → {len(current)}). Re-run to re-confirm."
        )
        return 8

    # Rail 3: backup.
    ts = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    backup = _backup_db(backup_dir)

    # Rail 4 + extra rail (d): batched apply.
    console.print(
        f"[bold]Applying {expected_count} updates "
        "(single commit)…[/bold]"
    )
    errors = _apply_updates_strict(updates)
    if errors:
        console.print(
            "[red]Errors during bulk apply (batch NOT committed):[/red]"
        )
        for u, msg in errors:
            console.print(f"  ✗ {u.id}: {msg}")
        console.print(
            f"[red]Backup (unused but intact):[/red] cp {backup} "
            f"{paths.REKORDBOX_LIVE_DB}"
        )
        return 5

    # Rail 5: verify.
    console.print("[bold]Verifying readback…[/bold]")
    failures = _verify(updates)

    # Rail 6: reversal script (covers the full batch regardless of per-row
    # verify failures, so the user can always roll back).
    rev = _write_reversal_script(updates, backup, backup_dir, ts)
    console.print(f"[green]Reversal script → {rev}[/green]")
    console.print(f"[green]Full DB backup → {backup}[/green]")

    # Summary table.
    attempted = len(updates)
    failed = len(failures)
    succeeded = attempted - failed
    summary = Table(title="Bulk apply summary", show_lines=False)
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")
    summary.add_row("Attempted", str(attempted))
    summary.add_row("Succeeded (verified)", str(succeeded))
    summary.add_row(
        "Failed verification",
        f"[red]{failed}[/red]" if failed else "0",
    )
    console.print(summary)

    if failures:
        console.print("[red]Verification failures:[/red]")
        for u, msg in failures[:20]:
            console.print(f"  ✗ {u.id}: {msg}")
        if len(failures) > 20:
            console.print(f"  ... and {len(failures) - 20} more")
        console.print(
            f"[yellow]Reversal script covers all {attempted} rows; "
            f"restore with:[/yellow] python {rev}"
        )
        return 6

    _write_patch(updates, DEFAULT_APPLIED)
    console.print(f"[green]Applied manifest → {DEFAULT_APPLIED}[/green]")
    return 0


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
            f"MAX_LIVE_TRACKS={MAX_LIVE_TRACKS}. Use `--bulk` for larger "
            "batches, or re-run with fewer --tracks."
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
    _write_patch(updates, DEFAULT_APPLIED)
    console.print(f"[green]Applied manifest → {DEFAULT_APPLIED}[/green]")
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
        help="comma-separated RB IDs. Required for --live (unless --bulk); "
        "max 3. In --dry-run, omit to include all triple-validated rows.",
    )
    p.add_argument(
        "--bulk",
        action="store_true",
        help="apply ALL triple-validated rows still broken per "
        "rb_missing_files.csv. Mutually exclusive with --tracks and "
        "--dry-run. Requires --live + --i-understand-the-risks plus a "
        "typed count-confirmation prompt.",
    )
    p.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="located.csv path")
    p.add_argument("--patch-out", type=Path, default=DEFAULT_PATCH, help="patch.json path")
    p.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR, help="backup dir")
    p.add_argument(
        "--broken-csv",
        type=Path,
        default=DEFAULT_BROKEN_CSV,
        help="path to current rb_missing_files.csv (bulk-mode selection)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # --bulk mutual exclusivity.
    if args.bulk:
        if args.dry_run:
            console.print(
                "[red]--bulk is mutually exclusive with --dry-run.[/red] "
                "Aborting."
            )
            return 2
        if args.tracks:
            console.print(
                "[red]--bulk is mutually exclusive with --tracks.[/red] "
                "Aborting."
            )
            return 2
        if not args.live:
            console.print(
                "[red]--bulk requires --live.[/red] Aborting."
            )
            return 2
        if not args.i_understand_the_risks:
            console.print(
                "[red]--bulk requires --i-understand-the-risks.[/red] "
                "Aborting."
            )
            return 2
        all_updates = _load_updates(args.input)
        return _run_bulk(all_updates, args.broken_csv, args.backup_dir)

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
                "[red]--live requires --tracks[/red] (comma-separated, 1–3 IDs) "
                "or --bulk."
            )
            return 2
        if len(track_ids) > MAX_LIVE_TRACKS:
            console.print(
                f"[red]Refusing:[/red] {len(track_ids)} IDs > MAX_LIVE_TRACKS"
                f"={MAX_LIVE_TRACKS}. Use `--bulk` for larger batches."
            )
            return 2
        return _run_live(updates, args.backup_dir)

    # Dry run (default).
    return _run_dry_run(updates, args.patch_out)


if __name__ == "__main__":
    sys.exit(main())
