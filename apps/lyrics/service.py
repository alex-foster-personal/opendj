"""The shared fetch/cache path used by the lyrics CLI and library jobs."""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from apps.cloud import stem_index
from apps.cloud.lyrics_asr_source import LYRICS_ASR_NOT_FOUND
from apps.lyrics import cache
from apps.lyrics.asr_lines import group_asr_words_into_lines, parse_asr_words
from apps.lyrics.asr_source import (
    AsrLyricsProvider,
    LyricsAsrFetchError,
    LyricsAsrNotFoundError,
    language_to_iso3,
)
from apps.lyrics.cache import LyricLine, Lyrics
from apps.lyrics.fetch_verdicts import FetchVerdict, utc_now_iso, write_verdict

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


FetchOutcome = Literal["cached", "instrumental", "no_source"]


@dataclass(frozen=True)
class FetchResult:
    outcome: FetchOutcome
    lyrics: Lyrics | None = None
    hub_code: str | None = None
    hub_message: str | None = None


class LyricsFetchService:
    def __init__(
        self,
        data_dir: Path,
        provider: SyncedLyricsProvider | None = None,
        asr_provider: AsrLyricsProvider | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.provider = provider or LrclibProvider()
        self.asr_provider = asr_provider or AsrLyricsProvider(data_dir)

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
        write_verdict(
            self.data_dir,
            FetchVerdict(
                stable_id=track.stable_id,
                outcome="cached",
                source="lrclib",
                vocals_sha256=vocals_sha256_for_track(self.data_dir, track.stable_id),
                hub_code=None,
                hub_message=None,
                language_iso3=None,
                recorded_at=utc_now_iso(),
            ),
        )
        return lyrics

    def fetch_or_resolve(self, track: Track) -> FetchResult:
        path = cache.cache_path(self.data_dir, track.stable_id)
        cached = cache.load(path)
        if cached is not None:
            if cached.stable_id != track.stable_id:
                raise ValueError(f"lyrics-cache identity mismatch at {path}")
            return FetchResult(outcome="cached", lyrics=cached)
        synced = self.provider.fetch_synced(track)
        if synced is not None:
            lyrics = Lyrics(track.stable_id, "lrclib", parse_lrc_lines(synced))
            cache.write(path, lyrics)
            write_verdict(
                self.data_dir,
                FetchVerdict(
                    stable_id=track.stable_id,
                    outcome="cached",
                    source="lrclib",
                    vocals_sha256=vocals_sha256_for_track(self.data_dir, track.stable_id),
                    hub_code=None,
                    hub_message=None,
                    language_iso3=None,
                    recorded_at=utc_now_iso(),
                ),
            )
            return FetchResult(outcome="cached", lyrics=lyrics)
        vocals_sha256 = vocals_sha256_for_track(self.data_dir, track.stable_id)
        if vocals_sha256 is None:
            _refuse_before_stem_index_loaded(self.data_dir)
            write_verdict(
                self.data_dir,
                FetchVerdict(
                    stable_id=track.stable_id,
                    outcome="no_source",
                    source=None,
                    vocals_sha256=None,
                    hub_code=LYRICS_ASR_NOT_FOUND,
                    hub_message="track is not in the local stem bundle index",
                    language_iso3=None,
                    recorded_at=utc_now_iso(),
                ),
            )
            return FetchResult(
                outcome="no_source",
                hub_code=LYRICS_ASR_NOT_FOUND,
                hub_message="track is not in the local stem bundle index",
            )
        try:
            transcript = self.asr_provider.fetch_transcript(track.stable_id)
        except LyricsAsrNotFoundError as exc:
            write_verdict(
                self.data_dir,
                FetchVerdict(
                    stable_id=track.stable_id,
                    outcome="no_source",
                    source=None,
                    vocals_sha256=vocals_sha256,
                    hub_code=exc.code,
                    hub_message=exc.message,
                    language_iso3=None,
                    recorded_at=utc_now_iso(),
                ),
            )
            return FetchResult(
                outcome="no_source",
                hub_code=exc.code,
                hub_message=exc.message,
            )
        except LyricsAsrFetchError:
            raise
        parsed_words = parse_asr_words(list(transcript.words))
        if not parsed_words:
            write_verdict(
                self.data_dir,
                FetchVerdict(
                    stable_id=track.stable_id,
                    outcome="instrumental",
                    source=None,
                    vocals_sha256=vocals_sha256,
                    hub_code=None,
                    hub_message=None,
                    language_iso3=language_to_iso3(transcript.language),
                    recorded_at=utc_now_iso(),
                ),
            )
            return FetchResult(outcome="instrumental")
        lines = group_asr_words_into_lines(parsed_words)
        if not lines:
            write_verdict(
                self.data_dir,
                FetchVerdict(
                    stable_id=track.stable_id,
                    outcome="instrumental",
                    source=None,
                    vocals_sha256=vocals_sha256,
                    hub_code=None,
                    hub_message=None,
                    language_iso3=language_to_iso3(transcript.language),
                    recorded_at=utc_now_iso(),
                ),
            )
            return FetchResult(outcome="instrumental")
        lyrics = Lyrics(track.stable_id, "asr", lines)
        cache.write(path, lyrics)
        write_verdict(
            self.data_dir,
            FetchVerdict(
                stable_id=track.stable_id,
                outcome="cached",
                source="asr",
                vocals_sha256=vocals_sha256,
                hub_code=None,
                hub_message=None,
                language_iso3=language_to_iso3(transcript.language),
                recorded_at=utc_now_iso(),
            ),
        )
        return FetchResult(outcome="cached", lyrics=lyrics)

    def fetch_stable_id(self, stable_id: str) -> Lyrics:
        return self.fetch(load_track(self.data_dir / "state" / "state.db", stable_id))

    def fetch_or_resolve_stable_id(self, stable_id: str) -> FetchResult:
        return self.fetch_or_resolve(load_track(self.data_dir / "state" / "state.db", stable_id))


LyricsService = LyricsFetchService


LYRICS_STEM_INDEX_NOT_LOADED = "LYRICS_STEM_INDEX_NOT_LOADED"


def _refuse_before_stem_index_loaded(data_dir: Path) -> None:
    """Refuse to record ``no_source`` while this spoke has no stem index yet.

    An absent index cache reads as ``{}``, so every track looks "not in the
    index" and would get a ``no_source`` verdict with ``vocals_sha256=None``.
    :func:`apps.lyrics.fetch_verdicts.is_terminal_fresh` treats that verdict
    as final while the sha stays ``None``, so a lyrics run that races the
    boot-time index refresh (or runs while the hub is down) skipped those
    tracks for good on the Air. Raising the retryable
    :class:`LyricsAsrFetchError` writes no verdict: the job item fails loud
    and the next run asks again. A machine with no hydration transport at all
    (local mode) never gets an index, so there ``no_source`` stays the answer.
    """
    if stem_index.local_index_cache_path(data_dir).is_file():
        return
    from apps.cloud.stem_source import resolve_stem_hydration_source

    if resolve_stem_hydration_source(data_dir) is None:
        return
    raise LyricsAsrFetchError(
        LYRICS_STEM_INDEX_NOT_LOADED,
        "the stem bundle index has not been fetched from the hub yet, so this "
        "track's vocals cannot be looked up; retry once the index is loaded",
    )


def vocals_sha256_for_track(data_dir: Path, stable_id: str) -> str | None:
    entry = stem_index.load_cached_index(data_dir).get(stable_id)
    if not entry:
        return None
    for filename, digest in entry.items():
        if filename.startswith("vocals."):
            return digest
    return None


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
    problem = lookup_metadata_problem(title, artists_json, duration_ms)
    if problem is not None:
        raise ValueError(f"track {stable_id!r} {problem}")
    return Track(stable_id, _first_artist(artists_json, stable_id), title, round(duration_ms / 1000))


#: What a lyrics lookup needs from the row, and the words for each lack. A
#: lack is a fact about the ROW, which a tag re-read or an edit can change, so
#: it must never be stored as a verdict keyed on the audio file.
METADATA_PROBLEMS: tuple[str, ...] = (
    "has no title", "has no artist", "has invalid artists_json", "has invalid duration_ms",
)


def lookup_metadata_problem(title: object, artists_json: object, duration_ms: object) -> str | None:
    """Why this row cannot be looked up (one of :data:`METADATA_PROBLEMS`), or None."""
    if not isinstance(title, str) or not title.strip():
        return METADATA_PROBLEMS[0]
    try:
        _first_artist(artists_json, "")
    except ValueError as error:
        return METADATA_PROBLEMS[2] if "artists_json" in str(error) else METADATA_PROBLEMS[1]
    if not isinstance(duration_ms, int) or duration_ms <= 0:
        return METADATA_PROBLEMS[3]
    return None


def is_metadata_reason(reason: str) -> bool:
    """True for a no-source reason that came from the row's metadata."""
    return any(reason.endswith(problem) for problem in METADATA_PROBLEMS)


def ids_without_lookup_metadata(connection: sqlite3.Connection, stable_ids: Sequence[str]) -> set[str]:
    """The subset of ``stable_ids`` whose row cannot be looked up right now."""
    if not stable_ids:
        return set()
    wanted = set(stable_ids)
    rows = connection.execute(
        "SELECT stable_id, title, artists_json, duration_ms FROM tracks WHERE deleted_at IS NULL"
    ).fetchall()
    return {
        sid for sid, title, artists, duration in rows
        if sid in wanted and lookup_metadata_problem(title, artists, duration) is not None
    }


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


def _stamp_ms(match: re.Match[str]) -> int:
    fraction = (match.group("fraction") or "").ljust(3, "0")
    return (int(match.group("minutes")) * 60 + int(match.group("seconds"))) * 1000 + int(
        fraction
    )


def parse_lrc_lines(synced: str) -> tuple[LyricLine, ...]:
    """Line-level LRC to timed lines, in time order.

    Two shapes that are valid LRC and not errors: consecutive lines sharing
    one stamp (LRCLIB serves them, e.g. a backing vocal doubled at 01:53.97),
    and one line carrying several stamps, the compressed form of a repeated
    chorus, whose later stamps land after lines further down the file. So
    every stamp becomes a line, the result is sorted by time, and a line
    repeated verbatim at the same stamp is kept once.

    What stays an error is a line whose EARLIEST stamp is before the previous
    line's earliest stamp: file order and clock disagree, and nothing in the
    file says which one is right.
    """
    if _WORD_TIMESTAMP.search(synced):
        raise ValueError("word-level timestamps are not supported")
    lines: dict[LyricLine, None] = {}
    previous_ms = -1
    for raw_line in synced.splitlines():
        matches = list(_TIMESTAMP.finditer(raw_line))
        if not matches or matches[0].start() != 0:
            continue
        stamps = [_stamp_ms(match) for match in matches]
        if min(stamps) < previous_ms:
            raise ValueError(
                f"synced lyrics jump backwards: a line stamped {min(stamps)} ms "
                f"follows one stamped {previous_ms} ms"
            )
        previous_ms = min(stamps)
        text = raw_line[matches[-1].end() :].strip()
        if text:
            lines.update(dict.fromkeys(LyricLine(start_ms, text) for start_ms in stamps))
    if not lines:
        raise ValueError("synced lyrics contain no line-level timestamps")
    return tuple(sorted(lines, key=lambda line: line.start_ms))
