"""Parse the many input shapes users paste for a Spotify playlist.

Accepts URL / URI / bare id; returns the 22-char playlist id.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

__all__ = ["parse_playlist_identifier", "PLAYLIST_ID_RE"]


PLAYLIST_ID_RE: re.Pattern[str] = re.compile(r"^[0-9A-Za-z]{21,24}$")

_URL_PATH_RE: re.Pattern[str] = re.compile(
    r"^/(?:embed/)?playlist/([0-9A-Za-z]{21,24})/?$"
)

_URI_RE: re.Pattern[str] = re.compile(r"^spotify:playlist:([0-9A-Za-z]{21,24})$")


def parse_playlist_identifier(value: str) -> str:
    """Return the bare playlist id for any accepted input shape.

    Raises :class:`ValueError` on anything not recognised. Pure; no IO.
    """
    if not isinstance(value, str):
        raise ValueError(f"playlist identifier must be str, got {type(value).__name__}")

    s = value.strip()
    if not s:
        raise ValueError("empty playlist identifier")

    if PLAYLIST_ID_RE.match(s):
        return s

    m = _URI_RE.match(s)
    if m is not None:
        return m.group(1)

    if "://" not in s and s.startswith("open.spotify.com"):
        s = "https://" + s

    try:
        parsed = urlparse(s)
    except ValueError as exc:  # pragma: no cover -- urlparse is permissive
        raise ValueError(f"not a Spotify playlist URL: {value!r}") from exc

    if parsed.scheme in ("http", "https") and "spotify.com" in (parsed.netloc or ""):
        m = _URL_PATH_RE.match(parsed.path)
        if m is not None:
            return m.group(1)

    raise ValueError(
        f"not a Spotify playlist URL / URI / id: {value!r}. "
        "Expected https://open.spotify.com/playlist/<id>, "
        "spotify:playlist:<id>, or the bare 22-char id."
    )
