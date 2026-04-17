"""Tests for apps.spotify.url_parse."""
from __future__ import annotations

import pytest

from apps.spotify.url_parse import PLAYLIST_ID_RE, parse_playlist_identifier

_ID = "37i9dQZF1DXcBWIGoYBM5M"


@pytest.mark.requirement("CAT-01")
class TestParsePlaylistIdentifier:
    def test_bare_id_passes_through(self) -> None:
        assert parse_playlist_identifier(_ID) == _ID

    def test_strips_whitespace(self) -> None:
        assert parse_playlist_identifier(f"  {_ID}  ") == _ID

    def test_plain_https_url(self) -> None:
        assert parse_playlist_identifier(f"https://open.spotify.com/playlist/{_ID}") == _ID

    def test_url_with_si_tracking(self) -> None:
        assert parse_playlist_identifier(
            f"https://open.spotify.com/playlist/{_ID}?si=abc123def"
        ) == _ID

    def test_http_scheme(self) -> None:
        assert parse_playlist_identifier(f"http://open.spotify.com/playlist/{_ID}") == _ID

    def test_embed_url(self) -> None:
        assert parse_playlist_identifier(
            f"https://open.spotify.com/embed/playlist/{_ID}"
        ) == _ID

    def test_spotify_uri(self) -> None:
        assert parse_playlist_identifier(f"spotify:playlist:{_ID}") == _ID

    def test_schemeless_url(self) -> None:
        assert parse_playlist_identifier(f"open.spotify.com/playlist/{_ID}") == _ID

    def test_trailing_slash_allowed(self) -> None:
        assert parse_playlist_identifier(f"https://open.spotify.com/playlist/{_ID}/") == _ID

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError):
            parse_playlist_identifier("")
        with pytest.raises(ValueError):
            parse_playlist_identifier("   ")

    def test_rejects_non_string(self) -> None:
        with pytest.raises(ValueError):
            parse_playlist_identifier(None)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            parse_playlist_identifier(42)  # type: ignore[arg-type]

    def test_rejects_wrong_length(self) -> None:
        with pytest.raises(ValueError):
            parse_playlist_identifier("tooShort")
        with pytest.raises(ValueError):
            parse_playlist_identifier("a" * 40)

    def test_rejects_album_url(self) -> None:
        with pytest.raises(ValueError):
            parse_playlist_identifier(f"https://open.spotify.com/album/{_ID}")

    def test_rejects_track_uri(self) -> None:
        with pytest.raises(ValueError):
            parse_playlist_identifier(f"spotify:track:{_ID}")

    def test_rejects_non_spotify_url(self) -> None:
        with pytest.raises(ValueError):
            parse_playlist_identifier(f"https://example.com/playlist/{_ID}")


@pytest.mark.requirement("CAT-01")
def test_playlist_id_re_is_anchored() -> None:
    assert PLAYLIST_ID_RE.match(_ID)
    assert not PLAYLIST_ID_RE.match(f"prefix{_ID}")
    assert not PLAYLIST_ID_RE.match(f"{_ID}suffix")
