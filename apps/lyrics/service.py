"""The shared fetch/cache path used by the lyrics CLI and future API readers."""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from apps.lyrics import cache
from apps.lyrics.cache import LyricLine, Lyrics

LRCLIB_GET_URL = "https://lrclib.net/api/get"
USER_AGENT = "music-dj-tools-lyrics/1.0"
_TIMESTAMP = re.compile(r"\[(?P<minutes>\d+):(?P<seconds>\d{2})(?:\.(?P<fraction>\d{1,3}))?\]")
_WORD_TIMESTAMP = re.compile(r"<\d{1,3}:\d{2}(?:\.\d{1,3})?>")


@dataclass(frozen=True)
class Track:
    stable_id: str
    artist: str
    title: str
    duration_s: int


class SyncedLyricsProvider(Protocol):
    def fetch_synced(self, track: Track) -> str | None: ...


class LrclibProvider:
    """Keyless, duration-anchored LRCLIB reader. No paid provider is involved."""

    def fetch_synced(self, track: Track) -> str | None:
        params = urllib.parse.urlencode(
            {"artist_name": track.artist, "track_name": track.title, "duration": track.duration_s}
        )
        request = urllib.request.Request(
            f"{LRCLIB_GET_URL}?{params}", headers={"User-Agent": USER_AGENT}
        )
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                if response.status != 200:
                    raise RuntimeError(f"LRCLIB returned HTTP {response.status}")
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise RuntimeError(f"LRCLIB returned HTTP {error.code}") from error
        synced = payload.get("syncedLyrics") if isinstance(payload, dict) else None
        return synced if isinstance(synced, str) and synced.strip() else None


class LyricsService:
    def __init__(self, data_dir: Path, provider: SyncedLyricsProvider | None = None) -> None:
        self.data_dir = data_dir
        self.provider = provider or LrclibProvider()

    def fetch(self, track: Track) -> Lyrics:
        path = cache.cache_path(self.data_dir, track.stable_id)
        cached = cache.load(path)
        if cached is not None:
            if cached.stable_id != track.stable_id:
                raise ValueError(f"lyrics-cache identity mismatch at {path}")
            return cached
        synced = self.provider.fetch_synced(track)
        if synced is None:
            raise cache.LyricsUnavailableError(f"no synced lyrics found for {track.stable_id}")
        lyrics = Lyrics(track.stable_id, "lrclib", parse_lrc_lines(synced))
        cache.write(path, lyrics)
        return lyrics

    def fetch_stable_id(self, stable_id: str) -> Lyrics:
        return self.fetch(load_track(self.data_dir / "state" / "state.db", stable_id))


def load_track(state_db: Path, stable_id: str) -> Track:
    if not state_db.is_file():
        raise FileNotFoundError(f"state DB missing on disk: {state_db}")
    connection = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT title, artists_json, duration_ms FROM tracks"
            " WHERE stable_id = ? AND deleted_at IS NULL",
            (stable_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise ValueError(f"unknown stable_id {stable_id!r}")
    title, artists_json, duration_ms = row
    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"track {stable_id!r} has no title")
    artist = _first_artist(artists_json, stable_id)
    if not isinstance(duration_ms, int) or duration_ms <= 0:
        raise ValueError(f"track {stable_id!r} has invalid duration_ms")
    return Track(stable_id, artist, title, round(duration_ms / 1000))


def _first_artist(artists_json: object, stable_id: str) -> str:
    try:
        artists = json.loads(artists_json) if isinstance(artists_json, str) else []
    except json.JSONDecodeError as error:
        raise ValueError(f"track {stable_id!r} has invalid artists_json") from error
    if (
        not isinstance(artists, list)
        or not artists
        or not isinstance(artists[0], str)
        or not artists[0].strip()
    ):
        raise ValueError(f"track {stable_id!r} has no artist")
    return artists[0]


def parse_lrc_lines(synced: str) -> tuple[LyricLine, ...]:
    if _WORD_TIMESTAMP.search(synced):
        raise ValueError("word-level timestamps are not supported")
    lines: list[LyricLine] = []
    for raw_line in synced.splitlines():
        matches = list(_TIMESTAMP.finditer(raw_line))
        if not matches or matches[0].start() != 0:
            continue
        text = raw_line[matches[-1].end() :].strip()
        if not text:
            continue
        for match in matches:
            fraction = (match.group("fraction") or "").ljust(3, "0")
            start_ms = (
                int(match.group("minutes")) * 60 + int(match.group("seconds"))
            ) * 1000 + int(fraction)
            lines.append(LyricLine(start_ms, text))
    if not lines:
        raise ValueError("synced lyrics contain no line-level timestamps")
    previous_ms = -1
    for line in lines:
        if line.start_ms <= previous_ms:
            raise ValueError("synced lyrics timestamps must be strictly increasing")
        previous_ms = line.start_ms
    return tuple(lines)
