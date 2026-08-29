"""Watched Spotify playlist registry (durable re-run list).

File: ``data/spotify/watched-playlists.json``.

Kept separate from the state DB so agents and humans can edit URLs without
opening SQLite, and so the list survives a state restore from backup.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.shared.paths import DATA_DIR

from .url_parse import parse_playlist_identifier

__all__ = [
    "DEFAULT_WATCHED",
    "WATCHED_PATH",
    "WatchedPlaylist",
    "ensure_watched_defaults",
    "load_watched",
    "save_watched",
    "watched_ids",
]

WATCHED_PATH: Path = DATA_DIR / "spotify" / "watched-playlists.json"

# Four OTLF-family URLs the user asked to keep for future re-runs.
DEFAULT_WATCHED: tuple[tuple[str, str, str], ...] = (
    (
        "5O5Yko7b4gSVWZBAd666qQ",
        "https://open.spotify.com/playlist/5O5Yko7b4gSVWZBAd666qQ",
        "OTLF watched 1",
    ),
    (
        "2wxF6r21sxsoY9mjWt24nY",
        "https://open.spotify.com/playlist/2wxF6r21sxsoY9mjWt24nY",
        "OTLF watched 2",
    ),
    (
        "1GxhwEBSNpxMLoik4WsRuz",
        "https://open.spotify.com/playlist/1GxhwEBSNpxMLoik4WsRuz",
        "OTLF watched 3",
    ),
    (
        "1p933PP9if7Ac0Yn7tEaUi",
        "https://open.spotify.com/playlist/1p933PP9if7Ac0Yn7tEaUi",
        "OTLF watched 4",
    ),
)


@dataclass(frozen=True)
class WatchedPlaylist:
    id: str
    url: str
    label: str = ""
    notes: str = ""


def _entry_from_dict(raw: dict[str, Any]) -> WatchedPlaylist:
    pid = str(raw.get("id") or "").strip()
    url = str(raw.get("url") or "").strip()
    if not pid and url:
        pid = parse_playlist_identifier(url)
    if not pid:
        raise ValueError(f"watched playlist entry missing id: {raw!r}")
    if not url:
        url = f"https://open.spotify.com/playlist/{pid}"
    # Normalize id from URL when both present and disagree.
    try:
        from_url = parse_playlist_identifier(url)
        if from_url and from_url != pid:
            pid = from_url
    except ValueError:
        pass
    return WatchedPlaylist(
        id=pid,
        url=url,
        label=str(raw.get("label") or ""),
        notes=str(raw.get("notes") or ""),
    )


def load_watched(path: Path | None = None) -> list[WatchedPlaylist]:
    """Load the watched registry. Missing file -> empty list (call ensure)."""
    target = Path(path) if path is not None else WATCHED_PATH
    if not target.is_file():
        return []
    data = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"watched registry must be an object: {target}")
    items = data.get("playlists")
    if items is None:
        return []
    if not isinstance(items, list):
        raise TypeError(f"watched.playlists must be a list: {target}")
    out: list[WatchedPlaylist] = []
    seen: set[str] = set()
    for raw in items:
        if not isinstance(raw, dict):
            raise TypeError(f"watched entry must be an object: {raw!r}")
        entry = _entry_from_dict(raw)
        if entry.id in seen:
            continue
        seen.add(entry.id)
        out.append(entry)
    return out


def save_watched(
    entries: list[WatchedPlaylist],
    path: Path | None = None,
    *,
    description: str | None = None,
) -> Path:
    """Atomically write the watched registry."""
    target = Path(path) if path is not None else WATCHED_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "description": description
        or (
            "Watched Spotify playlists for re-import when new tracks appear. "
            "Keep URLs durable across agent runs."
        ),
        "playlists": [
            {
                "id": e.id,
                "url": e.url,
                "label": e.label,
                "notes": e.notes,
            }
            for e in entries
        ],
    }
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(target)
    return target


def ensure_watched_defaults(path: Path | None = None) -> list[WatchedPlaylist]:
    """Ensure the four OTLF URLs are present; create/merge the registry file."""
    target = Path(path) if path is not None else WATCHED_PATH
    existing = load_watched(target)
    by_id = {e.id: e for e in existing}
    changed = False
    for pid, url, label in DEFAULT_WATCHED:
        if pid not in by_id:
            by_id[pid] = WatchedPlaylist(id=pid, url=url, label=label)
            changed = True
    # Preserve existing order, append new defaults at end.
    ordered: list[WatchedPlaylist] = list(existing)
    existing_ids = {e.id for e in existing}
    for pid, _url, _label in DEFAULT_WATCHED:
        if pid not in existing_ids:
            ordered.append(by_id[pid])
    if changed or not target.is_file():
        save_watched(ordered, target)
    return ordered


def watched_ids(path: Path | None = None) -> list[str]:
    """Return just the Spotify playlist ids, ensuring defaults first."""
    return [e.id for e in ensure_watched_defaults(path)]
