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


class _FuzzyTermIndex:
    """Per-query memo of which distinct lyric words fuzzy-match each query term.

    A lyric corpus repeats a small vocabulary across many documents, so scoring
    each distinct word once per term (behind a length bound that rules out most
    words before ``SequenceMatcher`` runs) keeps a full-index pass bounded by the
    vocabulary size rather than by the total number of lyric tokens (#12002).
    """

    def __init__(self, terms: list[str], *, threshold: float) -> None:
        """Hold the query terms and an empty per-term word memo."""
        self._terms = terms
        self._threshold = threshold
        self._matched: list[set[str]] = [set() for _ in terms]
        self._seen: list[set[str]] = [set() for _ in terms]

    def _word_matches(self, term: str, word: str) -> bool:
        """Whether ``word`` reaches the threshold ratio against ``term``, cheapest checks first."""
        total = len(term) + len(word)
        if total == 0 or 2.0 * min(len(term), len(word)) / total < self._threshold:
            return False
        matcher = SequenceMatcher(None, term, word)
        return matcher.quick_ratio() >= self._threshold and matcher.ratio() >= self._threshold

    def text_matches(self, normalized_query: str, text: str) -> bool:
        """Same answer as :func:`lyric_text_matches_fuzzy`, memoised across documents."""
        lowered = text.lower()
        if normalized_query in lowered:
            return True
        words: set[str] | None = None
        for index, term in enumerate(self._terms):
            if term in lowered:
                continue
            if words is None:
                words = set(lowered.split())
            matched = self._matched[index]
            if not matched.isdisjoint(words):
                continue
            seen = self._seen[index]
            fresh = words - seen
            seen |= fresh
            hit = False
            for word in fresh:
                if self._word_matches(term, word):
                    matched.add(word)
                    hit = True
            if not hit:
                return False
        return True


def _fuzzy_index_hits(
    conn: sqlite3.Connection,
    query: str,
    *,
    exclude: set[str],
    limit: int | None,
) -> list[str]:
    """One memoised pass over committed index rows, stopping at ``limit`` when given."""
    normalized_query = _normalize_query(query)
    terms = _query_terms(query)
    if not normalized_query or not terms:
        return []
    index = _FuzzyTermIndex(terms, threshold=LYRIC_FUZZY_SIMILARITY_THRESHOLD)
    hits: list[str] = []
    for row in conn.execute("SELECT stable_id, searchable_text FROM lyrics_fts"):
        stable_id = str(row[0])
        if stable_id in exclude:
            continue
        if not index.text_matches(normalized_query, str(row[1])):
            continue
        hits.append(stable_id)
        if limit is not None and len(hits) >= limit:
            break
    return hits


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
    return _fuzzy_index_hits(conn, query, exclude=exclude, limit=limit)


def fuzzy_search_indexed_count(
    conn: sqlite3.Connection,
    query: str,
    *,
    exclude: set[str],
) -> int:
    """How many index rows fuzzy-match excluding ids already returned by FTS."""
    return len(_fuzzy_index_hits(conn, query, exclude=exclude, limit=None))


def merge_fts_and_fuzzy(
    conn: sqlite3.Connection,
    query: str,
    fts_ids: list[str],
    fts_total: int,
    *,
    limit: int,
) -> tuple[list[str], int]:
    """FTS hits first, then fuzzy-only index hits up to ``limit``, from a single index pass."""
    fts_set = set(fts_ids)
    remaining = max(0, limit - len(fts_ids))
    fuzzy_only = _fuzzy_index_hits(conn, query, exclude=fts_set, limit=None)
    return fts_ids + fuzzy_only[:remaining], fts_total + len(fuzzy_only)


__all__ = [
    "LYRIC_FUZZY_PARTIAL_LINE_RATIO",
    "LYRIC_FUZZY_SIMILARITY_THRESHOLD",
    "best_lyric_line",
    "fuzzy_search_indexed",
    "fuzzy_search_indexed_count",
    "lyric_text_matches_fuzzy",
    "merge_fts_and_fuzzy",
]
