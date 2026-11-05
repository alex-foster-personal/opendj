"""MIK key/tempo per file, read once and remembered by path + mtime + size (KEY-FALLBACK-01).

Playlist rows already read per-row files (preview strips, artwork), so they may
read a file's ID3 header here on a miss. The library index never touches a
file: it serves what is remembered. Remembered values persist in a sidecar
beside state.db so the index fills in after a playlist view read them once.

-Claude
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path

from apps.shared import id3v2
from apps.shared.mik_tags import MikTagValues, read_mik_values

log = logging.getLogger(__name__)

FILENAME: str = "mik-tags.json"
_lock = threading.Lock()
_memo: dict[Path, dict[str, dict[str, object]]] = {}


def _sidecar(state_db_path: Path) -> Path:
    return state_db_path.parent / FILENAME


def _load(state_db_path: Path) -> dict[str, dict[str, object]]:
    side = _sidecar(state_db_path)
    if side not in _memo:
        _memo[side] = json.loads(side.read_text(encoding="utf-8")) if side.is_file() else {}
    return _memo[side]


def _save(state_db_path: Path, doc: Mapping[str, object]) -> None:
    side = _sidecar(state_db_path)
    side.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=side.parent, prefix=f".{side.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(doc, handle)
    os.replace(tmp, side)


def _stamp(path: Path) -> list[int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return [st.st_mtime_ns, st.st_size]


def _fresh_entry(path: Path, entry: dict[str, object] | None) -> dict[str, object] | None:
    """The remembered entry when its stamp still matches, else one read now; None when the file is gone."""
    stamp = _stamp(path)
    if stamp is None:
        return None
    if entry is not None and entry.get("stamp") == stamp:
        return entry
    try:
        values = read_mik_values(path)
    except (id3v2.Id3Error, OSError) as exc:
        # One unreadable tag must not fail a whole playlist listing; it is
        # logged by name and remembered as "no MIK values" for this stamp.
        log.warning("mik tags: %s unreadable: %s", path, exc)
        values = MikTagValues(key=None, bpm=None)
    return {"stamp": stamp, "key": values.key, "bpm": values.bpm}


def mik_values(
    state_db_path: Path, paths: Mapping[str, str | None], *, read_missing: bool
) -> dict[str, MikTagValues]:
    """stable_id -> MIK values for each id whose file is known; reads files only when ``read_missing``."""
    out: dict[str, MikTagValues] = {}
    with _lock:
        doc = _load(state_db_path)
        changed = False
        for sid, raw in paths.items():
            if not raw or not raw.lower().endswith((".mp3", ".aif", ".aiff", ".wav")):
                continue
            entry = doc.get(raw)
            if read_missing:
                fresh = _fresh_entry(Path(raw), entry)
                if fresh is None:
                    continue
                if fresh is not entry:
                    doc[raw] = entry = fresh
                    changed = True
            elif entry is None:
                # The index never stats or opens a file: it serves what a row read remembered.
                continue
            key, bpm = entry.get("key"), entry.get("bpm")
            out[sid] = MikTagValues(
                key=key if isinstance(key, str) else None,
                bpm=float(bpm) if isinstance(bpm, (int, float)) else None,
            )
        if changed:
            _save(state_db_path, doc)
    return out
