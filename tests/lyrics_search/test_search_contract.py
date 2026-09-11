"""Lyric-search data boundary + result-latency KPI (LYRICS-02, issue #1342).

Part 1 of #935 builds no index and no search UI. This test enforces the
boundary Parts 2/3 must consume, and the result-latency KPI those parts must
not regress, against a real (measured) scan rather than an asserted number.
"""

from __future__ import annotations

import random
import string
import time

import pytest

from apps.lyrics.cache import LyricLine, Lyrics
from apps.lyrics.search_contract import (
    SEARCH_RESULT_LATENCY_P95_MS_MAX,
    LyricSearchDocument,
    build_search_document,
    find_matches,
)

#: PARITY-01's measured audio-present `djmdContent` count - the population
#: `tracks_on_disk()` treats as the lyrics-coverage denominator.
_PARITY_01_AUDIO_PRESENT_TRACK_COUNT = 2314
_SYNTHETIC_DOC_CHARS = 2000
_LATENCY_TRIALS = 20


def _lyrics(stable_id: str, source: str, lines: tuple[LyricLine, ...]) -> Lyrics:
    return Lyrics(stable_id=stable_id, source=source, lines=lines)


def test_build_search_document_lowercases_and_joins_lines() -> None:
    """If lines carry mixed case then the searchable text is joined and lowercased."""
    lyrics = _lyrics(
        "stable-1",
        "lrclib",
        (LyricLine(0, "Hello World"), LyricLine(1000, "Second LINE")),
    )

    document = build_search_document(lyrics)

    assert document == LyricSearchDocument(
        stable_id="stable-1",
        source="lrclib",
        line_count=2,
        searchable_text="hello world second line",
    )


def test_build_search_document_rejects_empty_lines() -> None:
    """If a Lyrics entry has no lines then the boundary refuses to build a document."""
    lyrics = _lyrics("stable-2", "lrclib", ())

    with pytest.raises(ValueError, match="no lines to index"):
        build_search_document(lyrics)


def test_find_matches_returns_stable_ids_containing_query() -> None:
    """If only one document's text contains the query then only its id is returned."""
    documents = [
        LyricSearchDocument("a", "lrclib", 1, "dancing in the moonlight"),
        LyricSearchDocument("b", "lrclib", 1, "walking on sunshine"),
    ]

    assert find_matches("moonlight", documents) == ["a"]
    assert find_matches("SUNSHINE", documents) == ["b"]
    assert find_matches("neither", documents) == []


def test_find_matches_rejects_empty_query() -> None:
    """If the query is blank then the boundary fails fast instead of matching everything."""
    with pytest.raises(ValueError, match="must not be empty"):
        find_matches("   ", [])


def _synthetic_corpus(count: int, chars: int) -> list[LyricSearchDocument]:
    rng = random.Random(1342)
    vocabulary = [
        "".join(rng.choices(string.ascii_lowercase, k=rng.randint(2, 9))) for _ in range(4000)
    ]
    documents = []
    for index in range(count):
        words: list[str] = []
        length = 0
        while length < chars:
            word = rng.choice(vocabulary)
            words.append(word)
            length += len(word) + 1
        documents.append(
            LyricSearchDocument(
                stable_id=f"synthetic-{index}",
                source="lrclib",
                line_count=1,
                searchable_text=" ".join(words),
            )
        )
    return documents


def test_result_latency_meets_the_kpi_budget_on_a_realistic_corpus() -> None:
    """If a query runs against a PARITY-01-sized corpus then p95 stays under budget.

    This measures the real reference scan wall-clock time - it does not
    assert a number, it derives one and checks it against the committed
    budget in ``search_contract.py``.
    """
    documents = _synthetic_corpus(_PARITY_01_AUDIO_PRESENT_TRACK_COUNT, _SYNTHETIC_DOC_CHARS)
    queries = ["zzznotfound", documents[100].searchable_text[50:58], "love"]

    samples_ms: list[float] = []
    for query in queries:
        for _ in range(_LATENCY_TRIALS):
            start = time.perf_counter()
            find_matches(query, documents)
            samples_ms.append((time.perf_counter() - start) * 1000)

    samples_ms.sort()
    p95_ms = samples_ms[int(len(samples_ms) * 0.95)]

    assert p95_ms < SEARCH_RESULT_LATENCY_P95_MS_MAX, (
        f"measured p95={p95_ms:.3f}ms exceeds budget "
        f"{SEARCH_RESULT_LATENCY_P95_MS_MAX}ms over "
        f"{_PARITY_01_AUDIO_PRESENT_TRACK_COUNT} documents"
    )
