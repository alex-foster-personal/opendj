"""Agent-native safety coverage for the META-05 CLI.

- if live mode lacks an explicit risk acknowledgement then provider IO is broken
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.spotify.client import SpotifyClient, parse_playlist_payload
from apps.streaming_transfer.cli import EXIT_OK, EXIT_SAFETY, _write_report, main
from apps.streaming_transfer.llm import OpenRouterBatchMatcher
from apps.streaming_transfer.soundcloud import SoundCloudClient
from tests.streaming_transfer.test_transfer import SPOTIFY_CAPTURE


class _SpotifyDouble:
    def fetch_playlist(self, playlist_id: str, *, use_cache: bool):
        payload = {
            **SPOTIFY_CAPTURE,
            "id": playlist_id,
            "_items": [SPOTIFY_CAPTURE["_items"][3]],
        }
        return parse_playlist_payload(payload)

    def close(self) -> None:
        pass


class _LlmDouble:
    def resolve(self, escalations: object) -> tuple[()]:
        return ()


def _make_soundcloud_double(output_path: Path | None = None) -> SoundCloudClient:
    class _Double:
        def search_tracks(self, source: object) -> tuple[()]:
            if output_path is not None:
                output_path.write_bytes(b"other run\n")
            return ()

        def create_playlist(self, *args: object, **kwargs: object) -> str:
            raise AssertionError("create_playlist must not run")

        def close(self) -> None:
            pass

    return _Double()  # type: ignore[return-value]


def test_live_transfer_refuses_before_reading_credentials(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = main(
        [
            "spotify-to-soundcloud",
            "ABCDEFGHIJKLMNOPQRSTUV",
            "--confidence-threshold",
            "0.70",
            "--duration-tolerance-ms",
            "2000",
            "--output",
            "transfer-report.json",
            "--sharing",
            "private",
            "--live",
        ]
    )

    assert result == EXIT_SAFETY
    assert "--live requires --i-understand-the-risks" in capsys.readouterr().err


def test_invalid_threshold_refuses_before_reading_credentials(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = main(
        [
            "spotify-to-soundcloud",
            "ABCDEFGHIJKLMNOPQRSTUV",
            "--confidence-threshold",
            "0",
            "--duration-tolerance-ms",
            "2000",
            "--output",
            "transfer-report.json",
            "--sharing",
            "private",
            "--dry-run",
        ]
    )

    assert result == EXIT_SAFETY
    assert "confidence threshold" in capsys.readouterr().err


@pytest.mark.requirement("META-05")
def test_report_writer_never_overwrites_existing_file(tmp_path: Path) -> None:
    """[if] a report path already exists [then] exclusive write refuses, [else stop]."""
    path = tmp_path / "report.json"
    path.write_bytes(b"original\n")
    with pytest.raises(FileExistsError):
        _write_report(path, {"a": 1}, overwrite=False)
    assert path.read_bytes() == b"original\n"
    assert sorted(entry.name for entry in tmp_path.iterdir()) == ["report.json"]
    _write_report(path, {"a": 1}, overwrite=True)
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}
    fresh = tmp_path / "new-report.json"
    _write_report(fresh, {"b": 2}, overwrite=False)
    assert json.loads(fresh.read_text(encoding="utf-8")) == {"b": 2}


@pytest.mark.requirement("META-05")
def test_live_zero_match_run_writes_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] a live run matches nothing [then] the CLI still writes its report, [else stop]."""
    output = tmp_path / "r.json"
    assert not output.exists()
    monkeypatch.setattr(OpenRouterBatchMatcher, "from_env", classmethod(lambda cls: _LlmDouble()))
    monkeypatch.setattr(SpotifyClient, "from_env", classmethod(lambda cls: _SpotifyDouble()))
    monkeypatch.setattr(
        SoundCloudClient, "from_env", classmethod(lambda cls: _make_soundcloud_double())
    )
    result = main(
        [
            "spotify-to-soundcloud",
            "ABCDEFGHIJKLMNOPQRSTUV",
            "--confidence-threshold",
            "0.70",
            "--duration-tolerance-ms",
            "2000",
            "--sharing",
            "private",
            "--output",
            str(output),
            "--live",
            "--i-understand-the-risks",
            "--confirm-playlist-id",
            "ABCDEFGHIJKLMNOPQRSTUV",
        ]
    )
    captured = capsys.readouterr()
    assert result == EXIT_OK
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["counts"]["matched"] == 0
    assert payload["target_playlist_id"] is None
    assert "no tracks matched" in captured.err


@pytest.mark.requirement("META-05")
def test_output_created_after_precheck_is_not_clobbered(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """[if] output appears after pre-check [then] the file is not clobbered, [else stop]."""
    output = tmp_path / "r.json"
    monkeypatch.setattr(OpenRouterBatchMatcher, "from_env", classmethod(lambda cls: _LlmDouble()))
    monkeypatch.setattr(SpotifyClient, "from_env", classmethod(lambda cls: _SpotifyDouble()))
    monkeypatch.setattr(
        SoundCloudClient,
        "from_env",
        classmethod(lambda cls: _make_soundcloud_double(output_path=output)),
    )
    result = main(
        [
            "spotify-to-soundcloud",
            "ABCDEFGHIJKLMNOPQRSTUV",
            "--confidence-threshold",
            "0.70",
            "--duration-tolerance-ms",
            "2000",
            "--sharing",
            "private",
            "--output",
            str(output),
            "--dry-run",
        ]
    )
    captured = capsys.readouterr()
    assert result == EXIT_SAFETY
    assert "output exists" in captured.err
    assert output.read_bytes() == b"other run\n"
