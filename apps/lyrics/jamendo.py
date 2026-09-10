"""JamendoLyrics MultiLang loader -- ground truth for word-level lyric alignment evals.

Dataset: data/datasets/jamendolyrics (pull: `uv run scripts/pull_jamendolyrics.py`).
Spec: specs/karaoke-lyrics-alignment.md.

Format facts (verified against the HF snapshot, Thu 27 Aug 2026):
- JamendoLyrics.csv: URL,Filepath,Artist,Title,Genre,LicenseType,Language,LyricOverlap,
  Polyphonic,NonLexical. `Filepath` is the mp3 basename; song name = its stem.
- annotations/words/<name>.csv: word_start,word_end,line_end floats; word_end is `nan`
  except on line-final words. Rows align 1:1 with lyrics/<name>.words.txt lines.
- annotations/lines/<name>.csv: start_time,end_time,lyrics_line (derived from words).
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DATASET_DIR = Path(__file__).resolve().parents[2] / "data" / "datasets" / "jamendolyrics"
EXPECTED_SONGS = 79


@dataclass(frozen=True)
class WordSpan:
    word: str
    start_s: float
    end_s: float | None  # annotated only on line-final words in this dataset
    line_final: bool


@dataclass(frozen=True)
class JamendoSong:
    name: str  # mp3 basename stem, the dataset-wide join key
    artist: str
    title: str
    language: str  # as in the CSV, e.g. 'English'
    lyric_overlap: bool
    polyphonic: bool
    non_lexical: bool
    mp3_path: Path
    words: tuple[WordSpan, ...]

    @property
    def word_starts_s(self) -> list[float]:
        return [w.start_s for w in self.words]


#-----------------------------------------------------------------------------


def _parse_bool(raw: str) -> bool:
    if raw not in ("true", "false"):
        raise ValueError(f"expected 'true'/'false', got {raw!r}")
    return raw == "true"


def _load_word_spans(words_csv: Path, words_txt: Path) -> tuple[WordSpan, ...]:
    words = words_txt.read_text(encoding="utf-8").split("\n")
    words = [w for w in words if w.strip()]
    with words_csv.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if len(rows) != len(words):
        raise ValueError(
            f"{words_csv.name}: {len(rows)} timestamp rows vs "
            f"{len(words)} words in {words_txt.name}"
        )
    spans: list[WordSpan] = []
    for word, row in zip(words, rows, strict=True):
        end = float(row["word_end"])
        line_end = float(row["line_end"])
        spans.append(
            WordSpan(
                word=word,
                start_s=float(row["word_start"]),
                end_s=None if math.isnan(end) else end,
                line_final=not math.isnan(line_end),
            )
        )
    return tuple(spans)


def load_songs(dataset_dir: Path = DEFAULT_DATASET_DIR) -> list[JamendoSong]:
    """Load all songs with word-level ground truth. Fails fast on any missing piece."""
    index_csv = dataset_dir / "JamendoLyrics.csv"
    if not index_csv.is_file():
        raise FileNotFoundError(
            f"{index_csv} missing -- run `uv run scripts/pull_jamendolyrics.py` first"
        )
    songs: list[JamendoSong] = []
    with index_csv.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = Path(row["Filepath"]).stem
            mp3_path = dataset_dir / "mp3" / row["Filepath"]
            if not mp3_path.resolve().is_file():
                raise FileNotFoundError(f"{name}: audio missing at {mp3_path}")
            songs.append(
                JamendoSong(
                    name=name,
                    artist=row["Artist"],
                    title=row["Title"],
                    language=row["Language"],
                    lyric_overlap=_parse_bool(row["LyricOverlap"]),
                    polyphonic=_parse_bool(row["Polyphonic"]),
                    non_lexical=_parse_bool(row["NonLexical"]),
                    mp3_path=mp3_path,
                    words=_load_word_spans(
                        dataset_dir / "annotations" / "words" / f"{name}.csv",
                        dataset_dir / "lyrics" / f"{name}.words.txt",
                    ),
                )
            )
    if len(songs) != EXPECTED_SONGS:
        raise ValueError(f"expected {EXPECTED_SONGS} songs, loaded {len(songs)}")
    return songs
