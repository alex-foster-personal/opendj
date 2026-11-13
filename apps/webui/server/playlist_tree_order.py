"""User order of the playlist tree (LIBTREE-01..03, ADR-NEW-playlist-tree-order-overlay).

state.db playlists are flat and carry no position of their own; the tree
showed them in rekordbox's djmdPlaylist Seq walk, with every other playlist
name-sorted below. This module is the layer that lets a user (or an agent)
pick the order instead: one JSON file, ``<data>/state/playlist-tree-order.json``,
holding ``{"order": [playlist_id, ...]}``.

It is an overlay, never a second source of truth for which playlists exist:

* ids in the file that no longer name a live playlist are skipped on read;
* live playlists missing from the file keep their old place AFTER the ordered
  ones (rekordbox Seq first, then the name-sorted rest), so a playlist created
  or imported after the last reorder still shows up.

``seq_for`` turns the overlay plus the rekordbox Seq into the one integer the
tree already sorts on (``PlaylistSummary.seq``), so the frontend needs no new
ordering rule.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path

FILENAME = "playlist-tree-order.json"

_WRITE_LOCK = threading.Lock()


class PlaylistTreeOrderError(ValueError):
    """The stored file or a requested order is malformed."""


def path_for(data_dir: Path) -> Path:
    return data_dir / "state" / FILENAME


def load_order(path: Path) -> list[str]:
    """The stored order, or ``[]`` when the user never reordered.

    A file that exists but does not parse is an error, never an empty order:
    reading it as empty would silently throw away the user's arrangement on
    the next write.
    """
    return load_id_list(path, "order", "playlist tree order")


def load_id_list(path: Path, key: str, what: str) -> list[str]:
    """The unique playlist ids stored under ``key`` in the JSON overlay at ``path``, or ``[]`` when it is missing."""
    # Codex P2 on #6186 (r4266167694): the metadata probes sit inside the
    # guarded read too, so a permission or I/O error on stat (or a parent that
    # is not a directory) is the contract error, never a raw 500. Only a plain
    # missing file means "never written".
    try:
        try:
            os.lstat(path)
        except FileNotFoundError:
            return []
        if not path.is_file():
            # LIBTREE-03 fail loud: a directory (or dangling link) in the
            # overlay's place is corruption, not "never written".
            raise PlaylistTreeOrderError(f"{path}: {what} is not a regular file")
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlaylistTreeOrderError(f"{path}: unreadable {what}: {exc}") from exc
    ids = raw.get(key) if isinstance(raw, dict) else None
    if not isinstance(ids, list) or not all(isinstance(pid, str) for pid in ids):
        raise PlaylistTreeOrderError(f"{path}: '{key}' must be a list of playlist ids")
    # LIBTREE-03: a stored list naming a playlist twice is corruption (the
    # routes refuse it); unique stale ids stay allowed.
    if len(set(ids)) != len(ids):
        dupes = sorted({pid for pid in ids if ids.count(pid) > 1})
        raise PlaylistTreeOrderError(f"{path}: playlist ids stored twice: {', '.join(dupes)}")
    return list(ids)


def validate_order(order: Sequence[str], live_ids: set[str]) -> list[str]:
    """Reject an order that names a playlist twice or one that does not exist.

    The order may be partial: playlists it leaves out fall in after it.
    """
    seen: set[str] = set()
    unknown: list[str] = []
    for pid in order:
        if pid in seen:
            raise PlaylistTreeOrderError(f"playlist {pid!r} appears twice in the order")
        seen.add(pid)
        if pid not in live_ids:
            unknown.append(pid)
    if unknown:
        raise PlaylistTreeOrderError(f"unknown playlist ids: {', '.join(sorted(unknown))}")
    return list(order)


def save_order(path: Path, order: Sequence[str]) -> None:
    """Replace the stored order atomically (same-dir temp file + os.replace)."""
    write_json_atomic(path, {"order": list(order)})


def write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    """Replace ``path`` with ``payload`` as LF-ended JSON, atomically (same-dir temp file + os.replace)."""
    text = json.dumps(payload, indent=2) + "\n"
    with _WRITE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
            os.replace(temp_name, path)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise


def seq_for(
    playlist_ids: Sequence[str],
    user_order: Sequence[str],
    vendor_seq: Mapping[str, int | None],
) -> dict[str, int | None]:
    """The tree position of each playlist: user order first, then rekordbox Seq.

    Returns ``None`` for a playlist with neither; the tree sorts those last,
    by name, as before.
    """
    position = {pid: index for index, pid in enumerate(user_order)}
    offset = len(position)
    out: dict[str, int | None] = {}
    for pid in playlist_ids:
        if pid in position:
            out[pid] = position[pid]
            continue
        seq = vendor_seq.get(pid)
        out[pid] = None if seq is None else offset + seq
    return out
