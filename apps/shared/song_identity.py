"""How two library rows are judged to be the same song, from their tags alone.

One normalization for every reader, so the enrich card's song counts
(``apps/webui/server/enrich_songs.py``, ENRICH-03) and genre sharing between
duplicates (``apps/genre_infer/groups.py``, GENRE-08) cannot drift apart:

* :func:`song_key`: title + artists + length in whole seconds. One RECORDING;
  a radio edit and an extended mix are two songs. Used where the audio matters.
* :func:`title_artists_key`: the same key without the length. One WORK; every
  edit and every copy of it. Used where only the artist and title are asked
  about, as genre inference does (its web search is ``"{artist} {title} genre"``).

Both are built from the same parts (:func:`_parts`) and return None when a part
is missing: a row with no title or no artist is never joined to another on its
tags. A key is an opaque string; nothing parses it.

-Claude
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Sequence


def norm(text: str) -> str:
    """NFKC, case-folded, whitespace collapsed."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def artists_from_json(raw: object) -> list[str]:
    """The artist names in a ``tracks.artists_json`` value: a JSON list, else none.

    A value that is not a JSON list names no artists, so the row is never joined
    to another on its tags: text that cannot be read is not identity.
    """
    try:
        value = json.loads(str(raw)) if raw else []
    except ValueError:
        return []
    return [str(a) for a in value] if isinstance(value, list) else []


def _parts(title: str | None, artists: Sequence[str]) -> tuple[str, list[str]] | None:
    """(normalized title, sorted set of normalized artists), or None when either is empty."""
    names = sorted({norm(a) for a in artists if a and norm(a)})
    if not title or not norm(title) or not names:
        return None
    return norm(title), names


def _encode(*parts: object) -> str:
    """One string per distinct parts: JSON, so no character in a tag can join two fields."""
    return json.dumps(parts, ensure_ascii=False, separators=(",", ":"))


def title_artists_key(title: str | None, artists: Sequence[str]) -> str | None:
    """Normalized title + the sorted set of normalized artists, or None when either is empty."""
    parts = _parts(title, artists)
    return None if parts is None else _encode(*parts)


def song_key(title: str | None, artists: Sequence[str], duration_ms: int | None) -> str | None:
    """The v1 song key, or None when a part is missing (no dedupe on metadata then)."""
    parts = _parts(title, artists)
    if parts is None or not duration_ms or duration_ms <= 0:
        return None
    return _encode(*parts, round(duration_ms / 1000))


__all__ = ["artists_from_json", "norm", "song_key", "title_artists_key"]
