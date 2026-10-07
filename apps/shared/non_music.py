"""Is this library row a sample rather than a music track? (LIBM-178)

rekordbox ships its Sampler banks (MERGE FX, GROOVE CIRCUIT, OSC_SAMPLER) as
ordinary audio files under ``.../rekordbox/Sampler/`` and lists them in its
collection with the genre "Loop Samples". Imported as they are, they sat in All
Tracks next to the songs and AutoPlay could pick one: the row the maintainer reported was
a 13 s GROOVE CIRCUIT preset under ``.../rekordbox/Sampler/``.

The classifier is pure and positive-rule only. A row is non-music when either
holds:

* ``sampler-path``: its file path contains a ``rekordbox/Sampler`` directory
  pair (any case, either slash).
* ``sample-genre``: its genre is one of rekordbox's sample genres.

Short duration alone is NOT a rule: radio idents, intros and short edits are
music. Rows under :data:`SHORT_CANDIDATE_MS` are only reported as candidates
(:func:`is_short_candidate`), never hidden.
"""

from __future__ import annotations

from typing import Any, Literal

#: The genres rekordbox gives its bundled sampler content, compared casefolded.
SAMPLE_GENRES = frozenset({"loop samples", "one shot samples", "one-shot samples"})

#: Under this a row is a non-music CANDIDATE, reported and never hidden.
SHORT_CANDIDATE_MS = 30_000

_SAMPLER_DIRS = "/rekordbox/sampler/"

NonMusicReason = Literal["sampler-path", "sample-genre"]


def _is_sampler_path(file_path: str | None) -> bool:
    if not file_path:
        return False
    # One substring test per row: the index classifies every library row.
    return _SAMPLER_DIRS in "/" + file_path.replace("\\", "/").casefold() + "/"


def _is_sample_genre(genre: str | None) -> bool:
    return bool(genre) and genre.strip().casefold() in SAMPLE_GENRES


def non_music_reason(track: Any, genre: str | None = None) -> NonMusicReason | None:
    """Why ``track`` is non-music, or None for a music track.

    ``track`` needs ``file_path`` and ``genre``. ``genre`` overrides the track's own
    when the caller holds a better one (the listing's display genre, which prefers
    the rekordbox value).
    """
    if _is_sampler_path(getattr(track, "file_path", None)):
        return "sampler-path"
    effective_genre = getattr(track, "genre", None) if genre is None else genre
    if _is_sample_genre(effective_genre):
        return "sample-genre"
    return None


def is_non_music(track: Any, genre: str | None = None) -> bool:
    """True when ``track`` is a sample, not a music track. See :func:`non_music_reason`."""
    return non_music_reason(track, genre) is not None


def is_short_candidate(track: Any) -> bool:
    """A music row shorter than :data:`SHORT_CANDIDATE_MS`: reported, never hidden."""
    duration = getattr(track, "duration_ms", None)
    return isinstance(duration, int) and 0 < duration < SHORT_CANDIDATE_MS and not is_non_music(track)


__all__ = [
    "SAMPLE_GENRES",
    "SHORT_CANDIDATE_MS",
    "NonMusicReason",
    "is_non_music",
    "is_short_candidate",
    "non_music_reason",
]
