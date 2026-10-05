"""Matched-phrase-in-context snippets for lyric search hits (Part 3 of #935,
issue #1344).

The FTS5 index's ``searchable_text`` (``apps.lyrics.search_contract``) is
deliberately lossy for matching - lowercased, line breaks collapsed - and its
own docstring says it exists to be matched against, not displayed. This
module re-reads the original cached ``Lyrics.lines`` for a hit and returns
the one line that best carries the query, so a search result shows a phrase
in context rather than a bare title or the whole transcript.
"""
from __future__ import annotations

from pathlib import Path

from apps.lyrics.asr_hallucination import servable_lyrics
from apps.lyrics.cache import LyricLine, cache_path, load


def _query_terms(query: str) -> list[str]:
    return [term.lower() for term in query.strip().split() if term]


def _best_line(lines: tuple[LyricLine, ...], terms: list[str]) -> LyricLine:
    """The line matching the most query terms, earliest line breaking ties.

    A query's terms can land on different lines (the FTS index matches
    across the whole flattened document, not one line), so this picks the
    single line that carries the most of them rather than the first line
    matching anything at all.
    """
    best_line = lines[0]
    best_score = -1
    for line in lines:
        lowered = line.text.lower()
        score = sum(1 for term in terms if term in lowered)
        if score > best_score:
            best_score = score
            best_line = line
    return best_line


def matched_snippet(data_dir: Path, stable_id: str, query: str) -> str | None:
    """The lyric line that best matches ``query`` for ``stable_id``.

    None means either the query has no terms, or the cache entry is gone or
    unreadable since it was indexed - a real, if rare, race between the
    background index and the cache. The caller drops the hit rather than
    render a snippet with nothing behind it, the same convention
    ``routes/search.py`` uses for a hit that outlives its track row.
    """
    terms = _query_terms(query)
    if not terms:
        return None
    lyrics = load(cache_path(data_dir, stable_id))
    # LYRICS-12: never quote a hallucination line; a hallucination-only entry
    # is no hit, even while an index built before schema v3 still holds it.
    lyrics = None if lyrics is None else servable_lyrics(lyrics)
    if lyrics is None:
        return None
    return _best_line(lyrics.lines, terms).text


__all__ = ["matched_snippet"]
