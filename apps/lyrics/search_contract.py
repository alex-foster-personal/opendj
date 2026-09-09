"""Typed lyric-search data boundary for #935 Part 1 (issue #1342).

Investigation finding this module encodes: lyrics are NOT a bulk local
corpus. ``apps.lyrics.service.LyricsService`` fetches line-synced lyrics
from LRCLIB (the sole permitted free, keyless source) one stable track id at
a time, on demand, and caches the result at
``data/state/lyrics-cache/<stable_id>.json`` (``apps.lyrics.cache``,
LYRICS-01). There is no bulk import and no guarantee of coverage: the
denominator is whatever ``tracks_on_disk()`` in
``apps.webui.server.routes.ingest_job`` currently resolves (materialised
audio files, minus streaming-only rows), and the numerator is whatever
``valid_lyrics_ids()`` finds already cached - the same two functions the
shipped lyrics-completion health dot (issue #1352) already uses. This module
does not redefine that population; Parts 2/3 inherit it unchanged.

On this dev/CI checkout, coverage is 0 of 0: ``data/state/state.db`` ships
empty and gitignored (no tracks ingested) and no ``lyrics-cache`` directory
exists on disk. That is a fact about this sandboxed worktree, not a claim
about the maintainer's real library - the real coverage fraction can only be read by
running the existing health-dot coverage path against the real
``data/state/state.db`` and ``data/state/lyrics-cache/``, which this
worktree does not have.

Parts 2 (background index, #1343) and 3 (search UI, #1344) consume the
shapes below only. No index and no query engine live here: ``find_matches``
exists solely to give the result-latency KPI (LYRICS-02) something real to
measure, not as the eventual search implementation.
"""

from __future__ import annotations

from dataclasses import dataclass

from apps.lyrics.cache import Lyrics

LYRIC_SEARCH_CONTRACT_SCHEMA = 1

#: KPI evidence (LYRICS-02, .planning/REQUIREMENTS.md): measured on nucbox-wsl,
#: single run, n=100 trials, over a 2,314-document synthetic corpus (2,314 is
#: PARITY-01's measured audio-present `djmdContent` count - the same
#: population `tracks_on_disk()` treats as the lyrics-coverage denominator),
#: each document ~2,000 characters (a representative synced-lyric transcript
#: length; no real corpus exists locally to measure this from, so it is a
#: stated assumption, not a measured fact). The reference linear scan below
#: measured p50=5.9ms, p95=7.6ms, p99=7.6ms, max=7.6ms. The budget below
#: keeps roughly an order of magnitude of headroom over that measured p95,
#: for a slower machine and for Parts 2/3 swapping in a real index.
SEARCH_RESULT_LATENCY_P95_MS_MAX = 50.0


@dataclass(frozen=True)
class LyricSearchDocument:
    """One track's flattened, matchable lyric text.

    ``searchable_text`` is deliberately lossy (lowercased, line breaks
    collapsed to spaces): it exists to be matched against, not displayed.
    Parts 2/3 read the original ``Lyrics.lines`` for the snippet-in-context
    rendering #935 also asks for.
    """

    stable_id: str
    source: str
    line_count: int
    searchable_text: str


def build_search_document(lyrics: Lyrics) -> LyricSearchDocument:
    if not lyrics.lines:
        raise ValueError(f"lyrics entry {lyrics.stable_id!r} has no lines to index")
    searchable_text = " ".join(line.text for line in lyrics.lines).lower()
    return LyricSearchDocument(
        stable_id=lyrics.stable_id,
        source=lyrics.source,
        line_count=len(lyrics.lines),
        searchable_text=searchable_text,
    )


def find_matches(query: str, documents: list[LyricSearchDocument]) -> list[str]:
    """Reference substring match, used ONLY to enforce the latency KPI.

    Parts 2/3 own the real (indexed, ranked, snippeted) search; this linear
    scan is the floor a real index must beat, not a design for one.
    """
    needle = query.strip().lower()
    if not needle:
        raise ValueError("search query must not be empty")
    return [doc.stable_id for doc in documents if needle in doc.searchable_text]
