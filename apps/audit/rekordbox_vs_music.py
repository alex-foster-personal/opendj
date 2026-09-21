"""Audit: compare Rekordbox master.db tracks against filesystem audio files.

Run from the project root with the venv active:

    python -m apps.audit.rekordbox_vs_music

Outputs:
  * Rich summary table
  * ``data/rb_missing_files.csv``  — RB rows whose FolderPath file is gone
  * ``data/fs_files_not_in_rb.csv`` — files on disk not referenced by RB
  * ``data/rb_all_tracks.csv``     — every RB row with its classification
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.table import Table

from apps.shared import audio_files, paths, rekordbox_db

console = Console(width=120)


@dataclass(slots=True)
class Buckets:
    streaming: list[rekordbox_db.RBTrack]
    empty_path: list[rekordbox_db.RBTrack]
    file_linked_ok: list[rekordbox_db.RBTrack]
    file_linked_missing: list[rekordbox_db.RBTrack]

    @property
    def total(self) -> int:
        return (
            len(self.streaming)
            + len(self.empty_path)
            + len(self.file_linked_ok)
            + len(self.file_linked_missing)
        )


def _classify(tracks: list[rekordbox_db.RBTrack]) -> Buckets:
    streaming: list[rekordbox_db.RBTrack] = []
    empty_path: list[rekordbox_db.RBTrack] = []
    ok: list[rekordbox_db.RBTrack] = []
    missing: list[rekordbox_db.RBTrack] = []
    for t in tracks:
        fp = t.folder_path or ""
        if not fp:
            empty_path.append(t)
        elif t.is_streaming:
            streaming.append(t)
        elif t.file_path is not None and t.file_path.exists():
            ok.append(t)
        else:
            missing.append(t)
    return Buckets(
        streaming=streaming,
        empty_path=empty_path,
        file_linked_ok=ok,
        file_linked_missing=missing,
    )


def _category(t: rekordbox_db.RBTrack) -> str:
    if not t.folder_path:
        return "empty_path"
    if t.is_streaming:
        return "streaming"
    if t.file_path is not None and t.file_path.exists():
        return "file_ok"
    return "file_missing"


def _write_rb_missing_csv(out: Path, rows: list[rekordbox_db.RBTrack]) -> None:
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "title", "artist", "album", "folder_path"])
        for t in rows:
            w.writerow([t.id, t.title, t.artist, t.album, t.folder_path])


def _write_fs_missing_csv(out: Path, rows: list[audio_files.AudioFile]) -> None:
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["path", "size_bytes", "mtime"])
        for f in rows:
            w.writerow([str(f.path), f.size_bytes, f"{f.mtime:.0f}"])


def _write_rb_all_csv(out: Path, rows: list[rekordbox_db.RBTrack]) -> None:
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            ["id", "category", "title", "artist", "album", "folder_path", "file_size"]
        )
        for t in rows:
            w.writerow(
                [
                    t.id,
                    _category(t),
                    t.title,
                    t.artist,
                    t.album,
                    t.folder_path,
                    t.file_size or "",
                ]
            )


def _print_summary(buckets: Buckets, fs_total: int, fs_not_in_rb: int) -> None:
    total = buckets.total
    table = Table(title="Rekordbox vs Filesystem", show_lines=False)
    table.add_column("Bucket", style="bold")
    table.add_column("Count", justify="right")

    table.add_row("Rekordbox tracks total", f"{total}")
    table.add_row("  ├─ file-linked (OK)", f"{len(buckets.file_linked_ok)}")
    table.add_row(
        "  ├─ file-linked (MISSING) ⚠",
        f"[red]{len(buckets.file_linked_missing)}[/red]",
    )
    table.add_row("  ├─ streaming (Spotify etc)", f"{len(buckets.streaming)}")
    table.add_row("  └─ empty path", f"{len(buckets.empty_path)}")
    table.add_row("Filesystem audio files", f"{fs_total}")
    table.add_row("Files NOT in Rekordbox", f"[yellow]{fs_not_in_rb}[/yellow]")
    console.print(table)


def _rb_linked_paths(buckets: Buckets) -> set[Path]:
    """Build RB-linked path set (OK + MISSING)."""
    rb_linked: set[Path] = set()
    for t in buckets.file_linked_ok:
        p = t.file_path
        if p is None:
            continue
        try:
            rb_linked.add(p.resolve())
        except OSError:
            rb_linked.add(p)
    for t in buckets.file_linked_missing:
        if t.file_path is not None:
            rb_linked.add(t.file_path)
    return rb_linked


def _fs_by_resolved(fs_files: list[audio_files.AudioFile]) -> dict[Path, audio_files.AudioFile]:
    """Map resolved filesystem paths to AudioFile records."""
    fs_by_resolved: dict[Path, audio_files.AudioFile] = {}
    for f in fs_files:
        try:
            fs_by_resolved[f.path.resolve()] = f
        except OSError:
            fs_by_resolved[f.path] = f
    return fs_by_resolved


def main() -> None:
    console.print("[bold]Step 1:[/bold] copying live DBs → data/")
    copied = paths.copy_live_dbs()
    for name, path in copied.items():
        state = str(path) if path else "[dim]not found[/dim]"
        console.print(f"  • {name}: {state}")

    console.print("[bold]Step 2:[/bold] reading Rekordbox tracks")
    db = rekordbox_db.open_db()
    tracks = list(rekordbox_db.iter_tracks(db))
    console.print(f"  • {len(tracks)} tracks")
    buckets = _classify(tracks)

    console.print(f"[bold]Step 3:[/bold] scanning {paths.MUSIC_ROOTS}")
    fs_files = list(audio_files.scan_music_files())
    console.print(f"  • {len(fs_files)} audio files on disk")

    rb_linked = _rb_linked_paths(buckets)
    fs_by_resolved = _fs_by_resolved(fs_files)
    files_not_in_rb = [
        f for resolved, f in fs_by_resolved.items() if resolved not in rb_linked
    ]

    console.print("[bold]Step 4:[/bold] writing CSVs")
    paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _write_rb_missing_csv(paths.DATA_DIR / "rb_missing_files.csv", buckets.file_linked_missing)
    _write_fs_missing_csv(paths.DATA_DIR / "fs_files_not_in_rb.csv", files_not_in_rb)
    _write_rb_all_csv(paths.DATA_DIR / "rb_all_tracks.csv", tracks)

    _print_summary(buckets, len(fs_files), len(files_not_in_rb))

    if buckets.file_linked_missing:
        console.print("\n[bold red]First 10 RB entries with missing files:[/bold red]")
        for t in buckets.file_linked_missing[:10]:
            console.print(f"  • {t.title!r} — {t.artist!r} — {t.folder_path}")

    if files_not_in_rb:
        console.print("\n[bold yellow]First 10 filesystem files not in Rekordbox:[/bold yellow]")
        for f in files_not_in_rb[:10]:
            console.print(f"  • {f.path}")


if __name__ == "__main__":
    main()
