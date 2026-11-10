"""Fuzzy lyric matching for misremembered words and partial lines (issue #11145).

FTS5 prefix search in :mod:`apps.lyrics.search_index` stays the fast path for exact
and prefix token matches. This module scores queries against indexed
``searchable_text`` (``apps.lyrics.search_contract``) and original ``Lyrics.lines``
for snippets when FTS alone would miss a hit.

Only tracks present in the on-disk lyric index are candidates: there is no
fallback that reads the cache for unindexed tracks or synthesizes lyric text.
"""

from __future__ import annotations

import sqlite3
from difflib import SequenceMatcher

from apps.lyrics.cache import LyricLine, Lyrics

#: Minimum ``difflib.SequenceMatcher`` ratio between the full query (or a single
#: query term) and a lyric word for a misspelling to count as a match.
#: Measured intent: ``moonlght`` -> ``moonlight`` lands at ~0.89; ``moonlite``
#: -> ``moonlight`` at ~0.88; unrelated four-letter noise against real words
#: stays well below 0.5.
LYRIC_FUZZY_SIMILARITY_THRESHOLD = 0.72

#: Shorter queries use the same floor; partial-line matching also accepts a
#: whole-query ratio against the flattened searchable text when every token
#: already matches fuzzily.
LYRIC_FUZZY_PARTIAL_LINE_RATIO = 0.72


def _normalize_query(query: str) -> str:
    return " ".join(query.strip().lower().split())


def _query_terms(query: str) -> list[str]:
    normalized = _normalize_query(query)
    if not normalized:
        return []
    return normalized.split()


def _word_ratio(needle: str, haystack_word: str) -> float:
    return SequenceMatcher(None, needle, haystack_word).ratio()


def _term_matches_text(term: str, text: str, *, threshold: float) -> bool:
    if term in text:
        return True
    best = 0.0
    for word in text.split():
        best = max(best, _word_ratio(term, word))
        if best >= threshold:
            return True
    return False


def lyric_text_matches_fuzzy(query: str, searchable_text: str) -> bool:
    """Whether ``query`` fuzzy-matches indexed flattened lyric text."""
    normalized_query = _normalize_query(query)
    if not normalized_query:
        return False
    text = searchable_text.lower()
    if normalized_query in text:
        return True
    terms = _query_terms(query)
    if not terms:
        return False
    return all(_term_matches_text(term, text, threshold=LYRIC_FUZZY_SIMILARITY_THRESHOLD) for term in terms)


def _line_match_score(line: LyricLine, terms: list[str]) -> float:
    lowered = line.text.lower()
    score = 0.0
    for term in terms:
        if term in lowered:
            score += 2.0
            continue
        best_word = 0.0
        for word in lowered.split():
            best_word = max(best_word, _word_ratio(term, word))
        if best_word >= LYRIC_FUZZY_SIMILARITY_THRESHOLD:
            score += best_word
    return score


def best_lyric_line(lyrics: Lyrics, query: str) -> LyricLine | None:
    """The line from real ``Lyrics.lines`` that best carries ``query``, or None."""
    terms = _query_terms(query)
    if not terms or not lyrics.lines:
        return None
    best_line: LyricLine | None = None
    best_score = 0.0
    for line in lyrics.lines:
        score = _line_match_score(line, terms)
        if score > best_score:
            best_score = score
            best_line = line
    if best_line is None or best_score <= 0.0:
        return None
    return best_line


def fuzzy_search_indexed(
    conn: sqlite3.Connection,
    query: str,
    *,
    exclude: set[str],
    limit: int,
) -> list[str]:
    """Fuzzy-stable-id hits from committed index rows, never from the cache alone."""
    if limit <= 0:
        return []
    normalized_query = _normalize_query(query)
    if not normalized_query:
        return []
    rows = conn.execute("SELECT stable_id, searchable_text FROM lyrics_fts").fetchall()
    hits: list[str] = []
    for row in rows:
        stable_id = str(row[0])
        if stable_id in exclude:
            continue
        searchable_text = str(row[1])
        if not lyric_text_matches_fuzzy(normalized_query, searchable_text):
            continue
        hits.append(stable_id)
        if len(hits) >= limit:
            break
    return hits


def fuzzy_search_indexed_count(
    conn: sqlite3.Connection,
    query: str,
    *,
    exclude: set[str],
) -> int:
    """How many index rows fuzzy-match excluding ids already returned by FTS."""
    normalized_query = _normalize_query(query)
    if not normalized_query:
        return 0
    rows = conn.execute("SELECT stable_id, searchable_text FROM lyrics_fts").fetchall()
    count = 0
    for row in rows:
        stable_id = str(row[0])
        if stable_id in exclude:
            continue
        if lyric_text_matches_fuzzy(normalized_query, str(row[1])):
            count += 1
    return count


def merge_fts_and_fuzzy(
    conn: sqlite3.Connection,
    query: str,
    fts_ids: list[str],
    fts_total: int,
    *,
    limit: int,
) -> tuple[list[str], int]:
    """FTS hits first, then fuzzy-only index hits up to ``limit``."""
    fts_set = set(fts_ids)
    remaining = max(0, limit - len(fts_ids))
    fuzzy_extra = fuzzy_search_indexed(conn, query, exclude=fts_set, limit=remaining)
    merged = fts_ids + fuzzy_extra
    fuzzy_only_total = fuzzy_search_indexed_count(conn, query, exclude=fts_set)
    total = fts_total + fuzzy_only_total
    return merged, total


__all__ = [
    "LYRIC_FUZZY_PARTIAL_LINE_RATIO",
    "LYRIC_FUZZY_SIMILARITY_THRESHOLD",
    "best_lyric_line",
    "fuzzy_search_indexed",
    "fuzzy_search_indexed_count",
    "lyric_text_matches_fuzzy",
    "merge_fts_and_fuzzy",
]
