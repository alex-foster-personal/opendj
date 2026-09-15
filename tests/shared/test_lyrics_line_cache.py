"""Line-level lyric fetch/cache and CLI parity contract for issue #1191."""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.lyrics import __main__ as cli
from apps.lyrics import cache
from apps.lyrics.service import LyricLine, Lyrics, LyricsService, Track


class RecordingProvider:
    def __init__(self, synced: str | None) -> None:
        self.synced = synced
        self.calls = 0

    def fetch_synced(self, track: Track) -> str | None:
        self.calls += 1
        return self.synced


def test_lrc_fetch_is_cached_by_stable_id_and_never_refetched(tmp_path: Path) -> None:
    """If the same stable track id is fetched twice then the provider runs once."""
    track = Track("stable-track", "Artist", "Title", 180)
    provider = RecordingProvider("[00:01.20]First line\n[01:02.003]Second line")
    service = LyricsService(tmp_path, provider)
    first = service.fetch(track)
    second = service.fetch(track)
    assert provider.calls == 1
    assert first == second
    assert [(line.start_ms, line.text) for line in first.lines] == [
        (1200, "First line"),
        (62003, "Second line"),
    ]
    assert cache.cache_path(tmp_path, track.stable_id).is_file()


def test_unsynced_or_missing_provider_result_is_an_explicit_failure(tmp_path: Path) -> None:
    """If a source has no synced lines then no fabricated lyric cache is written."""
    track = Track("stable-track", "Artist", "Title", 180)
    with pytest.raises(cache.LyricsUnavailableError, match="no synced lyrics"):
        LyricsService(tmp_path, RecordingProvider(None)).fetch(track)
    assert not cache.cache_path(tmp_path, track.stable_id).exists()


def test_lrc_parser_rejects_word_level_timestamps(tmp_path: Path) -> None:
    """If an LRC line exposes enhanced word timestamps then fetch refuses karaoke data."""
    track = Track("stable-track", "Artist", "Title", 180)
    with pytest.raises(ValueError, match="word-level"):
        LyricsService(tmp_path, RecordingProvider("[00:01.00]<00:01.00>word <00:01.20>two")).fetch(
            track
        )


def test_lrc_parser_expands_multiple_line_timestamps(tmp_path: Path) -> None:
    """If one LRC text line has two timestamps then both become timed line records."""
    track = Track("stable-track", "Artist", "Title", 180)
    lyrics = LyricsService(tmp_path, RecordingProvider("[00:01.00][00:02.00]Repeated line")).fetch(
        track
    )
    assert [(line.start_ms, line.text) for line in lyrics.lines] == [
        (1000, "Repeated line"),
        (2000, "Repeated line"),
    ]


def test_fetch_command_uses_the_programmatic_fetch_path(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    """If the CLI fetches a track then it delegates to the same LyricsService path."""
    observed: dict[str, object] = {}

    class FakeService:
        def __init__(self, data_dir: Path) -> None:
            observed["data_dir"] = data_dir

        def fetch_or_resolve_stable_id(self, stable_id: str):
            from apps.lyrics.service import FetchResult

            observed["stable_id"] = stable_id
            lyrics = Lyrics(stable_id, "lrclib", (LyricLine(1000, "One line"),))
            return FetchResult(outcome="cached", lyrics=lyrics)

    monkeypatch.setattr(cli, "LyricsService", FakeService)
    assert cli.main(["fetch", "track-123", "--data-dir", str(tmp_path)]) == 0
    assert observed == {"data_dir": tmp_path, "stable_id": "track-123"}
    assert '"stable_id": "track-123"' in capsys.readouterr().out
