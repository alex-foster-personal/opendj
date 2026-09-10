"""Hand-curated word-span failure-mode tags for JamendoLyrics songs.

Tags live in `annotations/jamendolyrics.yaml` next to this module: per song a
list of `{tag, words: [start, end], note}` where `words` are 0-based INCLUSIVE
indices into `<name>.words.txt`; omitting `words` tags the whole song. The
loader validates shape and the caller validates indices against the dataset --
both fail fast, a malformed annotation is never silently dropped.

Purpose: quantify failure modes separately (non-lexical runs, bridge detaches,
separation artifacts) so `python -m apps.lyrics score --by-tag` gives each its
own honest denominator instead of one blended number.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_ANNOTATIONS_PATH = Path(__file__).resolve().parent / "annotations" / "jamendolyrics.yaml"

#-----------------------------------------------------------------------------


@dataclass(frozen=True)
class SpanTag:
    tag: str
    note: str
    start_idx: int | None  # None = whole song
    end_idx: int | None  # inclusive; None = whole song

    def word_indices(self, n_words: int) -> range:
        if self.start_idx is None:
            return range(n_words)
        assert self.end_idx is not None
        return range(self.start_idx, self.end_idx + 1)


def load_annotations(path: Path = DEFAULT_ANNOTATIONS_PATH) -> dict[str, list[SpanTag]]:
    if not path.is_file():
        raise FileNotFoundError(f"annotations file missing: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError(f"{path}: top level must be a mapping of song name -> tag list")
    annotations: dict[str, list[SpanTag]] = {}
    for song_name, entries in raw.items():
        annotations[song_name] = _song_tags(path, song_name, entries)
    return annotations


#-----------------------------------------------------------------------------


def _song_tags(path: Path, song_name: str, entries: object) -> list[SpanTag]:
    """Validate and parse one song's tag list. A malformed entry raises."""
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{path}: {song_name}: expected a non-empty list of tags")
    tags: list[SpanTag] = []
    for entry in entries:
        unknown_keys = set(entry) - {"tag", "words", "note"}
        if unknown_keys:
            raise ValueError(f"{path}: {song_name}: unknown keys {sorted(unknown_keys)}")
        if "tag" not in entry or "note" not in entry:
            raise ValueError(f"{path}: {song_name}: every entry needs 'tag' and 'note'")
        start_idx, end_idx = _span_word_indices(path, song_name, entry)
        tags.append(SpanTag(entry["tag"], entry["note"], start_idx, end_idx))
    return tags


def _span_word_indices(
    path: Path, song_name: str, entry: dict
) -> tuple[int | None, int | None]:
    """(start, end) 0-based inclusive word indices, or (None, None) for whole-song."""
    if "words" not in entry:
        return None, None
    words = entry["words"]
    if (
        not isinstance(words, list)
        or len(words) != 2
        or not all(isinstance(w, int) for w in words)
        or words[0] < 0
        or words[1] < words[0]
    ):
        raise ValueError(
            f"{path}: {song_name}: 'words' must be [start, end], "
            f"0 <= start <= end (got {words!r})"
        )
    return words[0], words[1]


def validate_against_songs(
    annotations: dict[str, list[SpanTag]], song_word_counts: dict[str, int]
) -> None:
    """Fail fast if a tag names an unknown song or an out-of-range word index."""
    unknown = sorted(set(annotations) - set(song_word_counts))
    if unknown:
        raise ValueError(f"annotations for unknown songs: {unknown}")
    for song_name, tags in annotations.items():
        n_words = song_word_counts[song_name]
        for span in tags:
            if span.end_idx is not None and span.end_idx >= n_words:
                raise ValueError(
                    f"{song_name}: tag {span.tag!r} ends at word {span.end_idx} "
                    f"but the song has {n_words} words"
                )
