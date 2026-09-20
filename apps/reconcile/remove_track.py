"""Remove Rekordbox track rows (and cascade dependents) from master.db.

Entry point::

    python -m apps.reconcile.remove_track [--dry-run | --live] \\
        [--i-understand-the-risks] --tracks ID[,ID[,ID]] \\
        [--db PATH] [--backup-dir PATH] [--reason "text"]

Safety rails for ``--live`` (all must pass or we abort):
  1. Typed ``yes i understand`` confirmation prompt.
  2. Refuse to run if Rekordbox is open (``pgrep -if rekordbox``).
  3. Timestamped backup of master.db before any write (size > 0 verified).
  4. Open the live DB, delete rows via pyrekordbox's ``delete()``, commit,
     then re-open and verify each row is gone.
  5. Emit a stand-alone ``restore-{ts}.py`` reversal script that re-inserts
     the removed content + cascade rows using values captured from the
     pre-delete state (idempotent — skips IDs that already exist).

Per-invocation cap: ``MAX_LIVE_TRACKS`` (3). No bulk mode for removals.

Cascade behaviour (verified against the plain RB fixture, see
``tests/fixtures/rekordbox/master.plain.db``):

  * pyrekordbox ``db.delete(content)`` automatically cascades to
    ``djmdCue`` and ``djmdMixerParam``.
  * It does NOT cascade to ``djmdSongPlaylist``, ``contentCue``, or
    ``contentFile`` — this script cleans those up via raw SQL after the
    ORM commit so playlist rows don't retain references to a ghost
    ``ContentID``.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table
from sqlalchemy import text

from apps.shared import paths, rekordbox_db
from apps.shared.rekordbox_writeback import require_writeback_enabled

console = Console(width=120)

DEFAULT_BACKUP_DIR: Path = paths.DATA_DIR / "reconcile" / "backups"
DEFAULT_PLAN_DIR: Path = paths.DATA_DIR / "reconcile"

CONFIRMATION_PHRASE = "yes i understand"
MAX_LIVE_TRACKS = 3

# Tables carrying a ``ContentID`` column that we must clean up manually
# after ``db.delete(content); db.commit()`` (pyrekordbox cascades
# djmdCue + djmdMixerParam for us, but not these).
#
# Order matters only if there were cross-table FKs; in practice there are
# none in master.db — we delete sequentially for simplicity.
MANUAL_CASCADE_TABLES: tuple[str, ...] = (
    "djmdSongPlaylist",
    "djmdSongHistory",
    "djmdSongHotCueBanklist",
    "djmdSongMyTag",
    "djmdSongRelatedTracks",
    "djmdSongSampler",
    "djmdSongTagList",
    "djmdActiveCensor",
    "djmdCloudExportSongPlaylist",
    "contentActiveCensor",
    "contentCue",
    "contentFile",
)


# ------------------------------------------------------------------ models


@dataclass(slots=True)
class Footprint:
    """The before-removal state for one RB track ID."""

    id: str
    exists: bool
    title: str = ""
    artist: str = ""
    folder_path: str = ""
    playlists: list[str] = field(default_factory=list)
    cue_points: int = 0
    beatgrid_entries: int = 0
    analysis_entries: int = 0
    mixer_param_entries: int = 0
    manual_cascade_counts: dict[str, int] = field(default_factory=dict)
    cascade_rows: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    content_row: dict[str, Any] = field(default_factory=dict)

    def to_plan_entry(self, reason: str) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "artist": self.artist,
            "folder_path": self.folder_path,
            "playlists": list(self.playlists),
            "cue_points": self.cue_points,
            "beatgrid_entries": self.beatgrid_entries,
            "analysis_entries": self.analysis_entries,
            "reason": reason,
        }


# ------------------------------------------------------------------ open


def _open_db(db_path: Path):
    """Open a Rekordbox DB via pyrekordbox.

    An encrypted DB goes through SQLCipher; a plain one must not, or
    SQLCipher reads ciphertext where the header is and reports "file is not
    a database". The header decides, via the one check in
    :func:`apps.shared.rekordbox_db.is_plain_sqlite`.
    """
    from pyrekordbox import Rekordbox6Database

    return Rekordbox6Database(
        path=str(db_path), unlock=not rekordbox_db.is_plain_sqlite(db_path)
    )


def _resolve_db_path(override: Path | None, *, live: bool) -> Path:
    """Return the DB path to operate on.

    * Explicit ``--db`` always wins.
    * In ``--live`` mode (no override) we ALWAYS go against
      ``paths.REKORDBOX_LIVE_DB`` — matching apply.py — so writes land
      in the real DB, never in the stale working copy.
    * In dry-run mode (no override) we refresh the working copy from
      live and read that instead.
    """
    if live:
        # Gate BEFORE the override branch, not after it. ``--db`` takes any
        # absolute path and ``_open_db`` sniffs the SQLite header and passes
        # ``unlock=True`` for an encrypted file, so ``--live --db <the real
        # master.db>`` is a fully working live-write lane that never mentions
        # ``paths.REKORDBOX_LIVE_DB``. Guarding only the constant branch left
        # that lane open while the suite stayed green.
        require_writeback_enabled("module.reconcile.remove_track")
    if override is not None:
        return Path(override)
    if live:
        return paths.REKORDBOX_LIVE_DB
    # Dry-run: read from a fresh snapshot of the live DB.
    paths.copy_live_dbs()
    if paths.REKORDBOX_WORKING_DB.exists():
        return paths.REKORDBOX_WORKING_DB
    return paths.REKORDBOX_LIVE_DB


# ------------------------------------------------------------------ probe


def _capture_footprint(db, track_id: str) -> Footprint:
    """Inspect one RB ID and return its dependency footprint.

    All SQL goes through pyrekordbox's SQLAlchemy engine so encrypted
    (pysqlcipher) and plain-SQLite fixtures work identically.
    """
    fp = Footprint(id=track_id, exists=False)
    row = db.get_content(ID=track_id)
    if row is None:
        return fp

    fp.exists = True
    fp.title = row.Title or ""
    fp.artist = getattr(getattr(row, "Artist", None), "Name", "") or ""
    fp.folder_path = row.FolderPath or ""

    # Snapshot the content row itself for the reversal script.
    content_rows = _rows_by_col(db, "djmdContent", "ID", track_id)
    fp.content_row = content_rows[0] if content_rows else {}

    # Playlist names via the ORM.
    names: list[str] = []
    for p in db.get_playlist():
        for s in getattr(p, "Songs", []) or []:
            if str(getattr(s, "ContentID", "")) == str(track_id):
                names.append(p.Name or "")
                break
    fp.playlists = sorted(set(names))

    # Counts via the engine.
    fp.cue_points = _count_by_col(db, "djmdCue", "ContentID", track_id)
    fp.mixer_param_entries = _count_by_col(db, "djmdMixerParam", "ContentID", track_id)
    fp.analysis_entries = _count_by_col(db, "contentFile", "ContentID", track_id)
    # No dedicated djmdBeatGrid table on this schema — beatgrid data is
    # carried inline on djmdMixerParam.
    fp.beatgrid_entries = fp.mixer_param_entries

    # Row dump for the reversal script — both manual-cascade and
    # ORM-cascade tables.
    for tbl in MANUAL_CASCADE_TABLES:
        rows = _rows_by_col(db, tbl, "ContentID", track_id)
        fp.manual_cascade_counts[tbl] = len(rows)
        if rows:
            fp.cascade_rows[tbl] = rows
    for tbl in ("djmdCue", "djmdMixerParam"):
        rows = _rows_by_col(db, tbl, "ContentID", track_id)
        if rows:
            fp.cascade_rows[tbl] = rows

    return fp


def _rows_by_col(db, table: str, col: str, value: str) -> list[dict[str, Any]]:
    """Return rows where ``col == value`` as list-of-dicts via the engine."""
    try:
        with db.engine.connect() as con:
            result = con.execute(
                text(f'SELECT * FROM "{table}" WHERE "{col}" = :v'),
                {"v": value},
            )
            return [dict(row._mapping) for row in result]
    except Exception:  # noqa: BLE001 — missing tables / driver issues
        return []


def _count_by_col(db, table: str, col: str, value: str) -> int:
    try:
        with db.engine.connect() as con:
            result = con.execute(
                text(f'SELECT COUNT(*) FROM "{table}" WHERE "{col}" = :v'),
                {"v": value},
            )
            return int(result.scalar() or 0)
    except Exception:  # noqa: BLE001
        return 0


# ------------------------------------------------------------------ preview


def _print_footprint_table(footprints: list[Footprint]) -> None:
    """Rich table of what each ID's removal would touch."""
    t = Table(title="Removal footprint", show_lines=True)
    t.add_column("ID", style="bold")
    t.add_column("Title")
    t.add_column("Artist")
    t.add_column("FolderPath")
    t.add_column("Playlists", overflow="fold")
    t.add_column("Cues", justify="right")
    t.add_column("Beatgrid", justify="right")
    t.add_column("Analysis", justify="right")
    t.add_column("Cascade", overflow="fold")
    for fp in footprints:
        if not fp.exists:
            t.add_row(
                fp.id,
                "[red]<not found>[/red]",
                "", "", "", "-", "-", "-", "-",
            )
            continue
        cascade_summary = ", ".join(
            f"{tbl}={n}"
            for tbl, n in sorted(fp.manual_cascade_counts.items())
            if n > 0
        ) or "-"
        t.add_row(
            fp.id,
            fp.title,
            fp.artist,
            f"[dim]{fp.folder_path}[/dim]",
            "\n".join(fp.playlists) if fp.playlists else "-",
            str(fp.cue_points),
            str(fp.beatgrid_entries),
            str(fp.analysis_entries),
            cascade_summary,
        )
    console.print(t)


def _write_plan(footprints: list[Footprint], reason: str, plan_dir: Path) -> Path:
    plan_dir.mkdir(parents=True, exist_ok=True)
    ts = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    out = plan_dir / f"remove-plan-{ts}.json"
    payload = [fp.to_plan_entry(reason) for fp in footprints if fp.exists]
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return out


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
        "[bold red]LIVE REMOVAL MODE.[/bold red] This will delete rows from "
        f"{paths.REKORDBOX_LIVE_DB} (and their cascade dependents)."
    )
    console.print(f"Type exactly [bold]{CONFIRMATION_PHRASE}[/bold] to proceed:")
    try:
        answer = input("> ").strip().lower()
    except EOFError:
        return False
    return answer == CONFIRMATION_PHRASE


def _backup_db(db_path: Path, backup_dir: Path) -> tuple[Path, str]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    dst = backup_dir / f"master.{ts}.db"
    shutil.copy2(db_path, dst)
    size = dst.stat().st_size
    if size <= 0:
        raise RuntimeError(f"Backup failed: {dst} has size {size}")
    console.print(f"[green]Backup OK[/green] → {dst} ({size:,} bytes)")
    return dst, ts


# ------------------------------------------------------------------ delete


def _apply_removals(
    db_path: Path,
    footprints: list[Footprint],
) -> list[tuple[str, str]]:
    """Delete each present track via pyrekordbox + manual cascade cleanup.

    Returns a list of ``(id, error)`` pairs for IDs that failed. An empty
    list means every requested ID was removed cleanly.
    """
    errors: list[tuple[str, str]] = []
    existing = [fp for fp in footprints if fp.exists]
    if not existing:
        return errors

    db = _open_db(db_path)
    try:
        for fp in existing:
            try:
                row = db.get_content(ID=fp.id)
            except Exception as exc:  # noqa: BLE001
                errors.append((fp.id, f"lookup failed: {exc}"))
                continue
            if row is None:
                errors.append((fp.id, "ID not found in live DB"))
                continue
            try:
                db.delete(row)
            except Exception as exc:  # noqa: BLE001
                errors.append((fp.id, f"delete failed: {exc}"))
                continue
        if not errors:
            db.commit()
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass

    if errors:
        return errors

    # Manual cascade cleanup via pyrekordbox's own engine (works for both
    # encrypted master.db and plain-SQLite fixtures).
    #
    # Two kinds of cleanup happen here:
    #   (a) Tables pyrekordbox doesn't touch at all (MANUAL_CASCADE_TABLES):
    #       delete rows by ContentID.
    #   (b) Tables pyrekordbox "cascades" by NULLing out ContentID rather
    #       than deleting the row (djmdCue, djmdMixerParam on this schema):
    #       remove those orphan rows by primary-key ID, using the IDs we
    #       captured in the footprint. If we skip (b), verification still
    #       passes (no rows match ContentID=<id>) but any reversal script
    #       fails with UNIQUE constraint on ID.
    db = _open_db(db_path)
    try:
        with db.engine.begin() as con:
            for fp in existing:
                for tbl in MANUAL_CASCADE_TABLES:
                    try:
                        con.execute(
                            text(f'DELETE FROM "{tbl}" WHERE "ContentID" = :v'),
                            {"v": fp.id},
                        )
                    except Exception as exc:  # noqa: BLE001
                        errors.append((fp.id, f"cascade {tbl}: {exc}"))
                # (b) clean pyrekordbox's NULL-ContentID orphans.
                for tbl in ("djmdCue", "djmdMixerParam"):
                    captured = fp.cascade_rows.get(tbl, [])
                    for row in captured:
                        row_id = row.get("ID")
                        if not row_id:
                            continue
                        try:
                            con.execute(
                                text(f'DELETE FROM "{tbl}" WHERE "ID" = :v'),
                                {"v": row_id},
                            )
                        except Exception as exc:  # noqa: BLE001
                            errors.append((fp.id, f"orphan-clean {tbl}: {exc}"))
    except Exception as exc:  # noqa: BLE001
        errors.append(("*", f"cascade commit failed: {exc}"))
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass
    return errors


def _verify_removed(db_path: Path, footprints: list[Footprint]) -> list[tuple[str, str]]:
    """Re-open the DB and assert every requested ID is gone + has no stragglers."""
    failures: list[tuple[str, str]] = []
    db = _open_db(db_path)
    try:
        for fp in footprints:
            if not fp.exists:
                continue
            try:
                row = db.get_content(ID=fp.id)
            except Exception as exc:  # noqa: BLE001
                failures.append((fp.id, f"post-delete lookup failed: {exc}"))
                continue
            if row is not None:
                failures.append((fp.id, "still present after delete"))
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass

    # Also check the manual cascade tables are clean.
    db2 = _open_db(db_path)
    try:
        for fp in footprints:
            if not fp.exists:
                continue
            for tbl in MANUAL_CASCADE_TABLES + ("djmdCue", "djmdMixerParam"):
                n = _count_by_col(db2, tbl, "ContentID", fp.id)
                if n:
                    failures.append((fp.id, f"orphans remain in {tbl}: {n}"))
    finally:
        try:
            db2.close()
        except Exception:  # noqa: BLE001
            pass
    return failures


# ------------------------------------------------------------------ reversal


def _write_reversal_script(
    footprints: list[Footprint],
    backup: Path,
    backup_dir: Path,
    ts: str,
    db_path: Path,
) -> Path:
    """Emit a self-contained restore-{ts}.py script.

    The script re-inserts the removed ``djmdContent`` rows plus every row
    we dumped from the manual-cascade tables. It's idempotent: it checks
    each primary-key column before inserting so re-running won't trip
    ``UNIQUE`` constraints.
    """
    out = backup_dir / f"restore-{ts}.py"

    # Build a compact payload the restore script can read directly.
    payload: dict[str, Any] = {
        "live_db": str(db_path),
        "backup": str(backup),
        "timestamp": ts,
        "entries": [],
    }
    for fp in footprints:
        if not fp.exists:
            continue
        payload["entries"].append({
            "id": fp.id,
            "content_row": fp.content_row,
            "cascade_rows": fp.cascade_rows,
        })

    # We embed the payload as a JSON string literal that the script parses at
    # runtime. This avoids Python/JSON literal mismatches (null vs None,
    # true/false vs True/False) and keeps the script self-contained.
    payload_json = json.dumps(payload, indent=2, ensure_ascii=False, default=str)

    body = '''#!/usr/bin/env python3
"""Auto-generated reversal for reconcile remove_track run __TS__.

Re-inserts the content rows + cascade dependents that were removed. If
this script itself fails, you can always fall back to the full DB
backup:

    cp "__BACKUP__" "__DB_PATH__"

This script is idempotent — it uses INSERT OR IGNORE so re-running
won't raise UNIQUE errors. It opens the DB via pyrekordbox so it works
against both the encrypted live master.db and plain-SQLite fixtures.
"""
from __future__ import annotations

import json
import sys

from pyrekordbox import Rekordbox6Database
from sqlalchemy import text

LIVE_DB = r"__DB_PATH__"
PAYLOAD = json.loads(r"""__PAYLOAD__""")


def _insert_row(con, table: str, row: dict) -> int:
    cols = list(row.keys())
    placeholders = ",".join([":c" + str(i) for i in range(len(cols))])
    collist = ",".join('"' + c + '"' for c in cols)
    sql = 'INSERT OR IGNORE INTO "' + table + '" (' + collist + ") VALUES (" + placeholders + ")"
    params = {"c" + str(i): row[c] for i, c in enumerate(cols)}
    result = con.execute(text(sql), params)
    return result.rowcount


def main() -> int:
    # Inlined on purpose. This script is self-contained by contract: it is the
    # fallback you run when the app is not importable, so it must not reach
    # into apps.shared.rekordbox_db for the header check the rest of the repo
    # shares. Prefix match rather than the full 16-byte magic, because a
    # backslash escape here would have to survive being written out through
    # this template and is one interpretation away from silently never
    # matching. No other format begins with these fifteen bytes.
    try:
        with open(LIVE_DB, "rb") as handle:
            header = handle.read(16)
    except OSError:
        header = b""
    db = Rekordbox6Database(
        path=LIVE_DB, unlock=not header.startswith(b"SQLite format 3")
    )
    try:
        total = 0
        with db.engine.begin() as con:
            for entry in PAYLOAD["entries"]:
                tid = entry["id"]
                content = entry.get("content_row") or {}
                if content:
                    n = _insert_row(con, "djmdContent", content)
                    print("  djmdContent id=" + str(tid) + ": +" + str(n))
                    total += n
                for tbl, rows in (entry.get("cascade_rows") or {}).items():
                    attempted = 0
                    for row in rows:
                        n = _insert_row(con, tbl, row)
                        total += n
                        attempted += n
                    print("  " + tbl + " id=" + str(tid) + ": +" + str(attempted) + " of " + str(len(rows)))
        print("Done. Rows inserted: " + str(total))
        return 0
    finally:
        try:
            db.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
'''
    body = (
        # as_posix(), not str(): a backslashed Windows path inside the
        # generated docstring is a SyntaxError (truncated \U escape), and
        # forward slashes are valid everywhere Python opens files.
        body.replace("__TS__", ts)
            .replace("__BACKUP__", Path(backup).as_posix())
            .replace("__DB_PATH__", Path(db_path).as_posix())
            .replace("__PAYLOAD__", payload_json)
    )
    out.write_text(body, encoding="utf-8")
    out.chmod(0o755)
    return out


# ------------------------------------------------------------------ flows


def _run_dry_run(
    db_path: Path,
    track_ids: list[str],
    reason: str,
    plan_dir: Path,
) -> int:
    db = _open_db(db_path)
    try:
        footprints = [_capture_footprint(db, tid) for tid in track_ids]
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass
    missing = [fp.id for fp in footprints if not fp.exists]
    if missing:
        console.print(
            f"[yellow]Warning:[/yellow] IDs not found in DB — will be skipped: "
            f"{', '.join(missing)}"
        )
    _print_footprint_table(footprints)
    plan = _write_plan(footprints, reason, plan_dir)
    console.print(f"[green]Wrote plan → {plan}[/green]")
    console.print("[dim]DRY-RUN — master.db untouched.[/dim]")
    return 0


def _run_live(
    db_path: Path,
    track_ids: list[str],
    reason: str,
    backup_dir: Path,
    plan_dir: Path,
) -> int:
    # Probe first (no writes).
    db = _open_db(db_path)
    try:
        footprints = [_capture_footprint(db, tid) for tid in track_ids]
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass

    _print_footprint_table(footprints)

    present = [fp for fp in footprints if fp.exists]
    if not present:
        console.print("[yellow]No matching IDs in the DB. Nothing to remove.[/yellow]")
        return 1

    # Rail 2: Rekordbox running?
    if _rekordbox_running():
        console.print(
            "[red]ABORT:[/red] Rekordbox is running. Quit Rekordbox completely "
            "(check the menu bar too) and retry. I will NOT auto-kill it."
        )
        return 3

    # Rail 1: typed confirmation.
    if not _confirm():
        console.print("[yellow]Confirmation not given. Aborting.[/yellow]")
        return 4

    # Rail 3: backup.
    backup, ts = _backup_db(db_path, backup_dir)

    # Rail 4: apply + cascade.
    console.print("[bold]Removing rows…[/bold]")
    errors = _apply_removals(db_path, footprints)
    if errors:
        console.print("[red]Errors during removal:[/red]")
        for tid, msg in errors:
            console.print(f"  ✗ {tid}: {msg}")
        console.print(f"[red]Restore with:[/red] cp {backup} {db_path}")
        return 5

    # Rail 5: verify.
    console.print("[bold]Verifying cascade…[/bold]")
    failures = _verify_removed(db_path, footprints)

    # Rail 6: reversal script (always emitted so user can always roll back).
    rev = _write_reversal_script(footprints, backup, backup_dir, ts, db_path)
    console.print(f"[green]Reversal script → {rev}[/green]")
    console.print(f"[green]Full DB backup → {backup}[/green]")

    # Also write the plan/audit alongside, so we have a record of what we did.
    _write_plan(footprints, reason, plan_dir)

    # Summary.
    ok = Table(title="Removal summary", show_lines=False)
    ok.add_column("ID", style="bold")
    ok.add_column("Title")
    ok.add_column("Status")
    failure_ids = {tid for tid, _ in failures}
    for fp in footprints:
        if not fp.exists:
            status = "[yellow]skipped (not found)[/yellow]"
        elif fp.id in failure_ids:
            status = "[red]failed[/red]"
        else:
            status = "[green]removed[/green]"
        ok.add_row(fp.id, fp.title or "-", status)
    console.print(ok)

    if failures:
        console.print("[red]Verification failures:[/red]")
        for tid, msg in failures:
            console.print(f"  ✗ {tid}: {msg}")
        console.print(
            f"[yellow]Reversal script restores everything; run:[/yellow] python {rev}"
        )
        return 6

    return 0


# ------------------------------------------------------------------ CLI


def _parse_ids(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    return [s.strip() for s in raw.split(",") if s.strip()]


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="apps.reconcile.remove_track",
        description="Remove Rekordbox track rows (+ cascade deps). Dry-run by default.",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="(default) preview + plan only")
    mode.add_argument("--live", action="store_true", help="actually delete from master.db")
    p.add_argument(
        "--i-understand-the-risks",
        action="store_true",
        help="required companion flag for --live",
    )
    p.add_argument(
        "--tracks",
        type=str,
        default=None,
        help=f"comma-separated RB IDs (1–{MAX_LIVE_TRACKS}). Required.",
    )
    p.add_argument(
        "--db",
        type=Path,
        default=None,
        help="override DB path (default: working copy, refreshed from live).",
    )
    p.add_argument(
        "--backup-dir",
        type=Path,
        default=DEFAULT_BACKUP_DIR,
        help="directory for pre-delete backups + reversal scripts",
    )
    p.add_argument(
        "--plan-dir",
        type=Path,
        default=DEFAULT_PLAN_DIR,
        help="where remove-plan-*.json files are written",
    )
    p.add_argument(
        "--reason",
        type=str,
        default="",
        help="free-text justification; recorded in the plan/audit JSON.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    track_ids = _parse_ids(args.tracks)
    if not track_ids:
        console.print("[red]--tracks is required[/red] (comma-separated RB IDs).")
        return 2
    if len(track_ids) > MAX_LIVE_TRACKS:
        console.print(
            f"[red]Refusing:[/red] {len(track_ids)} IDs > MAX_LIVE_TRACKS="
            f"{MAX_LIVE_TRACKS}. No bulk mode for removals — run again with fewer."
        )
        return 2

    db_path = _resolve_db_path(args.db, live=bool(args.live))

    if args.live:
        if not args.i_understand_the_risks:
            console.print(
                "[red]--live requires --i-understand-the-risks[/red]. Aborting."
            )
            return 2
        return _run_live(
            db_path,
            track_ids,
            args.reason,
            args.backup_dir,
            args.plan_dir,
        )

    # Dry-run (default).
    return _run_dry_run(db_path, track_ids, args.reason, args.plan_dir)


if __name__ == "__main__":
    sys.exit(main())
