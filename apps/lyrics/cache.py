"""Persistent contract for line-level synced lyrics.

Entries live at ``(private path)/{stable_id}.json``. Timestamps are
integer milliseconds at the cache boundary. The contract deliberately has no
word timestamps: karaoke belongs to neither this cache nor issue #1191.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.lyrics.json_parse import non_empty_string
from apps.shared.state.stable_id_paths import confined_stable_id_json_path

LYRICS_CACHE_SCHEMA = 1
LYRICS_CACHE_DIRNAME = "lyrics-cache"


class LyricsUnavailableError(RuntimeError):
    """The permitted free source has no line-synced lyrics for a track."""


@dataclass(frozen=True)
class LyricLine:
    start_ms: int
    text: str


@dataclass(frozen=True)
class Lyrics:
    stable_id: str
    source: str
    lines: tuple[LyricLine, ...]


def cache_dir(data_dir: Path) -> Path:
    return data_dir / "state" / LYRICS_CACHE_DIRNAME


def cache_path(data_dir: Path, stable_id: str) -> Path:
    return confined_stable_id_json_path(
        cache_dir(data_dir),
        stable_id,
        directory_name=LYRICS_CACHE_DIRNAME,
    )


def load(path: Path) -> Lyrics | None:
    if not path.is_file():
        return None
    return _parse(json.loads(path.read_text(encoding="utf-8")), path)


def write(path: Path, lyrics: Lyrics) -> None:
    payload = {
        "schema": LYRICS_CACHE_SCHEMA,
        "stable_id": lyrics.stable_id,
        "source": lyrics.source,
        "lines": [{"start_ms": line.start_ms, "text": line.text} for line in lyrics.lines],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as temporary:
        json.dump(payload, temporary, ensure_ascii=False, separators=(",", ":"))
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    try:
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _parse(value: Any, path: Path) -> Lyrics:
    if not isinstance(value, dict):
        raise TypeError(f"lyrics-cache entry {path} must be a JSON object")
    if value.get("schema") != LYRICS_CACHE_SCHEMA:
        raise ValueError(f"lyrics-cache entry {path} has unsupported schema")
    label = "lyrics-cache entry"
    stable_id = non_empty_string(value, "stable_id", path, entry_label=label)
    source = non_empty_string(value, "source", path, entry_label=label)
    return Lyrics(stable_id=stable_id, source=source, lines=_parse_lines(value.get("lines"), path))


def _parse_lines(value: Any, path: Path) -> tuple[LyricLine, ...]:
    lines = value
    if not isinstance(lines, list) or not lines:
        raise ValueError(f"lyrics-cache entry {path} has no line-level lyrics")
    parsed: list[LyricLine] = []
    previous_ms = -1
    for index, line in enumerate(lines):
        parsed_line = _parse_line(line, index, previous_ms, path)
        parsed.append(parsed_line)
        previous_ms = parsed_line.start_ms
    return tuple(parsed)


def _parse_line(value: Any, index: int, previous_ms: int, path: Path) -> LyricLine:
    if not isinstance(value, dict):
        raise TypeError(f"lyrics-cache entry {path} has invalid line {index}")
    start_ms, text = value.get("start_ms"), value.get("text")
    if not isinstance(start_ms, int) or isinstance(start_ms, bool) or start_ms < 0:
        raise ValueError(f"lyrics-cache entry {path} has invalid line {index}")
    # Equal stamps are valid LRC (two lines sung at once); only a line that
    # starts BEFORE the previous one is out of order.
    if start_ms < previous_ms or not isinstance(text, str) or not text.strip():
        raise ValueError(f"lyrics-cache entry {path} has invalid line {index}")
    return LyricLine(start_ms=start_ms, text=text)
