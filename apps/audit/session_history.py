"""Extract a Rekordbox play-session (History) as an ordered, key-annotated set list.

Run from the project root with the venv active::

    python -m apps.audit.session_history                # most recent session
    python -m apps.audit.session_history --history-id 1977580217
    python -m apps.audit.session_history --list         # list all sessions

What Rekordbox DOES record per play (proven via djmdSongHistory column dump):
  * TrackNo          -> play order (1 = first played)
  * ContentID        -> the track (native Title/Artist/BPM/Key/Rating)
  * created_at       -> per-track play time (distinct per row; the gap to the
                        next row approximates how long the track was ridden)

What Rekordbox does NOT record (so it cannot appear here):
  * played BPM / pitch-fader %      (ephemeral performance state)
  * live key shift / Master Key     (ephemeral)
  * in/out points actually used live (cue points exist, but not when hit)

Native key comes back already in Camelot (DjmdKey.ScaleName e.g. "9A"); we add
the classical key as a second column. Native BPM is the analysed tempo - the
"played at" / "+/-%" columns are intentionally blank with a NOT-RECORDED note.

Acceptance:
  [if] most recent HISTORY has N rows [then] we print N ordered rows or ⛔️
  [if] a row's Title is blank [then] we fall back to the file basename ⛔️
  [if] key is "9A" [then] classical column reads "Em" ⛔️
"""
from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.table import Table

from apps.shared import paths, rekordbox_db

console = Console(width=140)

# Camelot (alphanumeric) -> classical key name. Source of truth for the
# second key column. A-side = minor, B-side = major.
CAMELOT_TO_KEY: dict[str, str] = {
    "1A": "Abm", "2A": "Ebm", "3A": "Bbm", "4A": "Fm", "5A": "Cm", "6A": "Gm",
    "7A": "Dm", "8A": "Am", "9A": "Em", "10A": "Bm", "11A": "F#m", "12A": "C#m",
    "1B": "B", "2B": "F#", "3B": "Db", "4B": "Ab", "5B": "Eb", "6B": "Bb",
    "7B": "F", "8B": "C", "9B": "G", "10B": "D", "11B": "A", "12B": "E",
}


@dataclass(slots=True)
class PlayedTrack:
    order: int
    title: str
    artist: str
    bpm: float | None
    camelot: str
    classical: str
    rating: int
    folder_path: str
    content_id: str
    playlists: list[str]
    played_at: str  # HH:MM:SS local
    gap: str  # M:SS until next track loaded (~dwell time); "" for last
    is_streaming: bool


# ----- helpers ----------------------------------------------------------


def _index_track_object(obj: dict, idx: dict[str, tuple[str, str]]) -> None:
    tid, name, arts = obj.get("id"), obj.get("name"), obj.get("artists")
    if tid and name and arts and tid not in idx:
        try:
            idx[tid] = (", ".join(a.get("name", "") for a in arts), name)
        except Exception:
            pass


def _walk_spotify_cache(obj: object, idx: dict[str, tuple[str, str]]) -> None:
    if isinstance(obj, dict):
        _index_track_object(obj, idx)
        for value in obj.values():
            _walk_spotify_cache(value, idx)
    elif isinstance(obj, list):
        for value in obj:
            _walk_spotify_cache(value, idx)


def _spotify_name_index() -> dict[str, tuple[str, str]]:
    """Map Spotify track-id -> (artist, title) from local caches.

    Streaming tracks store only ``spotify:track:<id>`` in Rekordbox; the
    human-readable name is resolved live from Spotify at display time and is
    NOT persisted. We rebuild it from (a) ``session_resolved.json`` written by
    the API resolver and (b) every cached playlist payload under
    ``data/spotify/cache/``. File-backed so re-runs need no network.
    """
    idx: dict[str, tuple[str, str]] = {}
    sp_dir = paths.DATA_DIR / "spotify"

    resolved = sp_dir / "session_resolved.json"
    if resolved.exists():
        for tid, val in json.loads(resolved.read_text()).items():
            if isinstance(val, (list, tuple)) and len(val) == 2:
                idx[tid] = (val[0], val[1])

    cache_dir = sp_dir / "cache"
    if cache_dir.exists():
        for f in cache_dir.glob("*.json"):
            try:
                _walk_spotify_cache(json.loads(f.read_text()), idx)
            except Exception:
                continue
    return idx


def _content_id_to_playlists(db) -> dict[str, list[str]]:
    """Map ContentID -> curated playlist names (excludes History/auto nodes)."""
    index: dict[str, list[str]] = {}
    for pl in rekordbox_db.iter_playlists(db):
        name = pl.name
        if name.upper().startswith("HISTORY") or name.isdigit():
            continue  # skip session-history + year/month grouping nodes
        for cid in pl.track_ids:
            index.setdefault(cid, []).append(name)
    return index


def _most_recent_history(db):
    """Return the newest leaf HISTORY node (the one with actual songs)."""
    leaves = [
        h for h in db.get_history()
        if str(getattr(h, "Name", "")).upper().startswith("HISTORY")
    ]
    leaves.sort(key=lambda h: str(getattr(h, "created_at", "")), reverse=True)
    return leaves[0] if leaves else None


def _resolve(db, history_id: str) -> list[PlayedTrack]:
    rows = [
        s for s in db.get_history_songs()
        if str(getattr(s, "HistoryID", None)) == str(history_id)
    ]
    rows.sort(key=lambda s: (getattr(s, "TrackNo", 0) or 0))
    pl_index = _content_id_to_playlists(db)
    sp_index = _spotify_name_index()
    times = [getattr(s, "created_at", None) for s in rows]

    out: list[PlayedTrack] = []
    for i, s in enumerate(rows):
        cid = str(s.ContentID)
        c = db.get_content(ID=cid)
        c = c.first() if hasattr(c, "first") else c
        folder = (getattr(c, "FolderPath", "") or "")
        title = (getattr(c, "Title", "") or "").strip()
        artist = rekordbox_db._safe_name(getattr(c, "Artist", None))
        # Streaming tracks: title/artist live in Spotify, not the local DB.
        if not title and folder.startswith("spotify:"):
            sp_id = folder.split(":")[-1]
            if sp_id in sp_index:
                artist, title = sp_index[sp_id]
        if not title:
            title = f"[{Path(folder).name}]" if folder else f"<ContentID {cid}>"
        bpm_raw = getattr(c, "BPM", None)
        bpm = (int(bpm_raw) / 100.0) if bpm_raw else None
        key = getattr(c, "Key", None)
        camelot = (getattr(key, "ScaleName", "") or "").strip() or "-"
        classical = CAMELOT_TO_KEY.get(camelot, camelot if camelot != "-" else "-")
        t_now = times[i]
        played_at = t_now.strftime("%H:%M:%S") if t_now else "-"
        gap = ""
        if i + 1 < len(times) and times[i + 1] and t_now:
            secs = (times[i + 1] - t_now).total_seconds()
            gap = f"{int(secs // 60)}:{int(secs % 60):02d}"
        out.append(
            PlayedTrack(
                order=int(getattr(s, "TrackNo", 0) or 0),
                title=title,
                artist=artist,
                bpm=bpm,
                camelot=camelot,
                classical=classical,
                rating=int(getattr(c, "Rating", 0) or 0),
                folder_path=folder,
                content_id=cid,
                playlists=pl_index.get(cid, []),
                played_at=played_at,
                gap=gap,
                is_streaming=folder.startswith(("spotify:", "tidal:", "http")),
            )
        )
    return out


# ----- output -----------------------------------------------------------


def _stars(n: int) -> str:
    return ("★" * n) + ("·" * (5 - n)) if 0 <= n <= 5 else str(n)


def _print_table(name: str, tracks: list[PlayedTrack]) -> None:
    table = Table(title=f"{name}  ({len(tracks)} tracks, in play order)", show_lines=False)
    table.add_column("#", justify="right", style="bold")
    table.add_column("Time", justify="center", style="dim")
    table.add_column("Rode", justify="center", style="dim")
    table.add_column("Title", style="cyan", max_width=40)
    table.add_column("Artist", style="green", max_width=24)
    table.add_column("Cam", justify="center", style="magenta")
    table.add_column("Key", justify="center")
    table.add_column("BPM", justify="right")
    table.add_column("Pitch%", justify="center", style="dim")
    table.add_column("Rating")
    table.add_column("Src", justify="center")
    for t in tracks:
        table.add_row(
            str(t.order),
            t.played_at,
            t.gap or "—",
            t.title,
            t.artist or "-",
            t.camelot,
            t.classical,
            f"{t.bpm:.1f}" if t.bpm else "-",
            "n/r",  # played pitch% is NOT recorded by Rekordbox
            _stars(t.rating),
            "Spfy" if t.is_streaming else "file",
        )
    console.print(table)


def _history_slug(name: str) -> str:
    """Return a deterministic, path-safe filename stem for a History name."""
    basename = name.replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    slug = "".join(
        char if char.isascii() and (char.isalnum() or char in {"_", "-"}) else "_"
        for char in basename
    ).strip("_")
    return slug or "history"


def _save(name: str, tracks: list[PlayedTrack]) -> Path:
    out_dir = (paths.PROJECT_ROOT / "maintainer" / "sessions").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = (out_dir / f"{_history_slug(name)}.csv").resolve()
    if not csv_path.is_relative_to(out_dir):
        raise RuntimeError(f"Refusing to write session export outside {out_dir}: {csv_path}")
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["order", "played_at", "rode_for", "title", "artist",
                    "camelot", "classical_key", "native_bpm", "played_bpm",
                    "played_pct", "rating", "source", "folder_path", "playlists"])
        for t in tracks:
            w.writerow([t.order, t.played_at, t.gap, t.title, t.artist,
                        t.camelot, t.classical, f"{t.bpm:.2f}" if t.bpm else "",
                        "NOT_RECORDED", "NOT_RECORDED", t.rating,
                        "spotify" if t.is_streaming else "file",
                        t.folder_path, " | ".join(t.playlists)])
    return csv_path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--history-id", default=None, help="Specific HistoryID to dump")
    ap.add_argument("--list", action="store_true", help="List all sessions and exit")
    args = ap.parse_args()

    db = rekordbox_db.open_db(paths.REKORDBOX_LIVE_DB)

    if args.list:
        leaves = [h for h in db.get_history()
                  if str(getattr(h, "Name", "")).upper().startswith("HISTORY")]
        leaves.sort(key=lambda h: str(getattr(h, "created_at", "")), reverse=True)
        console.print(f"[bold]{len(leaves)} sessions:[/bold]")
        for h in leaves[:30]:
            console.print(f"  {getattr(h,'created_at','?')}  id={h.ID}  {getattr(h,'Name','?')}")
        return

    if args.history_id:
        hid = args.history_id
        name = f"HISTORY {hid}"
        hobj = next((h for h in db.get_history() if str(h.ID) == str(hid)), None)
        if hobj is not None:
            name = getattr(hobj, "Name", name)
    else:
        hobj = _most_recent_history(db)
        if hobj is None:
            console.print("[red]No HISTORY sessions found.[/red]")
            return
        hid = str(hobj.ID)
        name = getattr(hobj, "Name", f"HISTORY {hid}")

    tracks = _resolve(db, hid)
    if not tracks:
        console.print(f"[red]Session {name} (id={hid}) has no song rows.[/red]")
        return

    _print_table(name, tracks)
    csv_path = _save(name, tracks)
    console.print(f"\n[dim]Saved →[/dim] {csv_path}")
    console.print(
        "\n[bold]Played BPM / pitch% / live key-shift / in-out points are "
        "[red]NOT RECORDED[/red] by Rekordbox[/bold] - the djmdSongHistory table "
        "has no column for them. Only native (analysed) values are shown."
    )


if __name__ == "__main__":
    main()
