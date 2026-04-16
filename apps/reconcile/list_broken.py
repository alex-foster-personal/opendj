"""List every Rekordbox track whose ``FolderPath`` no longer resolves.

Entry point::

    python -m apps.reconcile.list_broken

Writes ``data/reconcile/broken.csv`` with one row per broken RB track and
prints a rich summary (count + top-5 parent directories by frequency).

Read-only: refreshes the working copy via :func:`paths.copy_live_dbs` and
opens it through :func:`shared.rekordbox_db.open_db`. Never touches the live
``master.db``.
"""
from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.table import Table

from apps.shared import paths, rekordbox_db

console = Console(width=120)

OUT_DIR: Path = paths.DATA_DIR / "reconcile"
OUT_CSV: Path = OUT_DIR / "broken.csv"

CSV_COLUMNS: tuple[str, ...] = (
    "id",
    "title",
    "artist",
    "album",
    "genre",
    "bpm",
    "key",
    "duration_s",
    "file_size",
    "original_path",
    "basename",
    "parent_dir",
)


@dataclass(slots=True)
class BrokenRow:
    """Row emitted to ``broken.csv`` — one per missing RB track."""

    id: str
    title: str
    artist: str
    album: str
    genre: str
    bpm: float | None
    key: str
    duration_s: int | None
    file_size: int | None
    original_path: str
    basename: str
    parent_dir: str

    def as_csv(self) -> list[str]:
        return [
            self.id,
            self.title,
            self.artist,
            self.album,
            self.genre,
            "" if self.bpm is None else f"{self.bpm:.2f}",
            self.key,
            "" if self.duration_s is None else str(self.duration_s),
            "" if self.file_size is None else str(self.file_size),
            self.original_path,
            self.basename,
            self.parent_dir,
        ]


def _collect_broken(db) -> list[BrokenRow]:
    """Iterate RB content and keep rows whose on-disk file is gone.

    Pulls extra fields (``KeyName``, ``Length``) directly off the ORM row
    rather than widening :class:`RBTrack` for fields only this module needs.
    """
    rows: list[BrokenRow] = []
    # Index by ID so we can marry the lean RBTrack view (file_path checks,
    # is_streaming, etc.) with the raw ORM row for extra fields.
    rb_tracks = {t.id: t for t in rekordbox_db.iter_tracks(db)}
    for raw in db.get_content():
        rb = rb_tracks.get(str(raw.ID))
        if rb is None:
            continue
        if rb.is_streaming or rb.file_path is None:
            continue
        if rb.file_path.exists():
            continue

        key = getattr(raw, "KeyName", None) or ""
        length = getattr(raw, "Length", None)
        duration_s: int | None
        try:
            duration_s = int(length) if length is not None else None
        except (TypeError, ValueError):
            duration_s = None

        original = rb.folder_path or ""
        basename = Path(original).name if original else ""
        parent = str(Path(original).parent) if original else ""

        rows.append(
            BrokenRow(
                id=rb.id,
                title=rb.title,
                artist=rb.artist,
                album=rb.album,
                genre=rb.genre,
                bpm=rb.bpm,
                key=key,
                duration_s=duration_s,
                file_size=rb.file_size,
                original_path=original,
                basename=basename,
                parent_dir=parent,
            )
        )
    return rows


def _write_csv(rows: list[BrokenRow], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_COLUMNS)
        for r in rows:
            w.writerow(r.as_csv())


def _print_summary(rows: list[BrokenRow]) -> None:
    table = Table(title="Broken Rekordbox links", show_lines=False)
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")
    table.add_row("Broken rows", f"[red]{len(rows)}[/red]")
    with_size = sum(1 for r in rows if r.file_size)
    table.add_row("  ├─ with file_size", f"{with_size}")
    with_key = sum(1 for r in rows if r.key)
    table.add_row("  ├─ with key", f"{with_key}")
    with_dur = sum(1 for r in rows if r.duration_s)
    table.add_row("  └─ with duration", f"{with_dur}")
    console.print(table)

    if not rows:
        return

    parent_counts = Counter(r.parent_dir for r in rows if r.parent_dir)
    top_table = Table(title="Top parent directories", show_lines=False)
    top_table.add_column("Count", justify="right", style="bold")
    top_table.add_column("Parent dir")
    for parent, count in parent_counts.most_common(5):
        top_table.add_row(str(count), parent)
    console.print(top_table)


def main() -> None:
    console.print("[bold]Step 1:[/bold] refreshing working DB copy")
    paths.copy_live_dbs()

    console.print("[bold]Step 2:[/bold] scanning Rekordbox content")
    db = rekordbox_db.open_db()
    rows = _collect_broken(db)
    console.print(f"  • {len(rows)} broken links")

    console.print(f"[bold]Step 3:[/bold] writing {OUT_CSV}")
    _write_csv(rows, OUT_CSV)

    _print_summary(rows)


if __name__ == "__main__":
    main()
