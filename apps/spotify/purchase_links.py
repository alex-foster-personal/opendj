"""Canonical purchase deep-links for a Spotify track we do not own.

One template table, used by the importer's persisted ``pending_tracks`` rows
and by the live listen-history pane. Both surfaces must offer the SAME five
stores in the SAME order, so the table lives here rather than in either
caller.

No store is queried and no availability is implied: these are search URLs the
operator opens in a browser to buy the track manually.
"""
from __future__ import annotations

from urllib.parse import quote_plus

__all__ = ["SUGGESTED_SOURCE_KEYS", "SUGGESTED_SOURCE_TEMPLATES", "suggested_sources"]

SUGGESTED_SOURCE_TEMPLATES: tuple[tuple[str, str], ...] = (
    ("beatport", "https://www.beatport.com/search?q={q}"),
    ("bandcamp", "https://bandcamp.com/search?q={q}&item_type=t"),
    ("qobuz", "https://www.qobuz.com/us-en/search?q={q}"),
    ("apple_music", "https://music.apple.com/us/search?term={q}"),
    ("discogs", "https://www.discogs.com/search?q={q}&type=release"),
)

SUGGESTED_SOURCE_KEYS: tuple[str, ...] = tuple(name for name, _ in SUGGESTED_SOURCE_TEMPLATES)


def suggested_sources(artist: str, title: str) -> dict[str, str]:
    """Build the five store search URLs for one ``artist`` / ``title`` pair."""
    query = quote_plus(f"{artist} {title}".strip())
    return {name: template.format(q=query) for name, template in SUGGESTED_SOURCE_TEMPLATES}
