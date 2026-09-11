"""Persistent contract for WORD-level karaoke timings (artifact kind
``karaoke_words``).

Entries live at ``data/state/karaoke-cache/{stable_id}.json``. This module is
the deliberate sibling of :mod:`apps.lyrics.cache`, which holds LINE-level
lyrics and carries no word timestamps by design: neither file widens into the
other (operational-plan D13.2).

Three rules this module exists to enforce.

1. **The writer owns ``idx``.** Indices are assigned positionally over the
   whole list, never read from the caller's dict. A partial word list would
   silently mis-time the back half of a song, so a write is always the whole
   list.
2. **Canonical bytes.** :func:`canonical_bytes` is ``json.dumps(obj,
   sort_keys=True, separators=(",", ":"), ensure_ascii=False)`` encoded UTF-8
   with NO trailing newline. ``lyric_verdict.words_content_hash`` is the
   sha256 of exactly those bytes, so it equals the sha256 of the file on disk
   and of the R2 object. Any change to the serialisation changes every hash,
   which is what :data:`PIPELINE_VERSION` records.
3. **The parser re-asserts what the retired ``lyric_word`` DDL enforced.**
   Three CHECK constraints, a NOT NULL and a composite primary key's
   uniqueness plus contiguity have no enforcement in JSON, so
   :func:`parse_words` performs all of them and rejects unknown keys. Without
   that the fail-fast posture degrades from "the database refused it" to "the
   file was written".
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.lyrics import lines

KARAOKE_CACHE_SCHEMA: int = 1
KARAOKE_CACHE_DIRNAME: str = "karaoke-cache"

#: Grammar ``<YYYY.MM.DD>-<round>``. Bump this whenever the aligner, the
#: witness or this writer changes the bytes a given track serialises to: it is
#: stored on the verdict row AND inside the artifact, so a row can always be
#: traced back to the code that produced its hash.
PIPELINE_VERSION: str = "2026.09.09-round3a"

#: The retired ``lyric_word.witness`` CHECK vocabulary (round-5 classes).
WITNESS_CLASSES: tuple[str, ...] = (
    "agree",
    "drift",
    "contradict",
    "lost",
    "unheard",
    "unmatchable",
)

#: Exactly the keys a serialised word carries. Fixed set in both directions:
#: the writer emits all of them, the parser rejects anything else.
WORD_KEYS: frozenset[str] = frozenset(
    {"idx", "word", "start_s", "end_s", "score", "witness", "asr_delta_s", "line_final"}
)

#: Keys an INPUT word mapping may carry. ``idx`` is absent because the writer
#: assigns it (rule 1). ``word`` and ``line_final`` are required; the five
#: value keys mean JSON null when absent, which is stated here rather than
#: hidden in a ``.get`` call.
INPUT_WORD_KEYS: frozenset[str] = WORD_KEYS - {"idx"}
REQUIRED_INPUT_WORD_KEYS: frozenset[str] = frozenset({"word", "line_final"})
_NULLABLE_NUMBER_KEYS: tuple[str, ...] = ("start_s", "end_s", "score", "asr_delta_s")


class KaraokeCacheError(ValueError):
    """A karaoke-words artifact could not be built, read or trusted."""


@dataclass(frozen=True)
class KaraokeWord:
    """One aligned word.

    Satisfies :class:`apps.lyrics.lines.WordRow` structurally; ``score`` and
    ``asr_delta_s`` ride as extra attributes rather than widening that
    Protocol, because line grouping has no use for either.
    """

    idx: int
    word: str
    start_s: float | None
    end_s: float | None
    score: float | None
    witness: str | None
    asr_delta_s: float | None
    line_final: bool


@dataclass(frozen=True)
class KaraokeWords:
    """One track's whole word list, self-describing."""

    stable_id: str
    source: str
    pipeline_version: str
    words: tuple[KaraokeWord, ...]


#-----------------------------------------------------------------------------
# paths
#-----------------------------------------------------------------------------
def cache_dir(data_dir: Path) -> Path:
    return data_dir / "state" / KARAOKE_CACHE_DIRNAME


def cache_path(data_dir: Path, stable_id: str) -> Path:
    """The artifact path for one track, refusing an id that escapes the dir."""
    root = cache_dir(data_dir).resolve()
    path = (root / f"{stable_id}.json").resolve()
    if path.parent != root:
        raise KaraokeCacheError(
            f"stable_id escapes karaoke-cache directory: {stable_id!r}"
        )
    return path


#-----------------------------------------------------------------------------
# building from caller input
#-----------------------------------------------------------------------------
def _input_number(entry: Mapping[str, Any], field: str, index: int) -> float | None:
    value = entry.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise KaraokeCacheError(
            f"word {index} field {field} must be a number or null, got {value!r}"
        )
    return float(value)


def _input_witness(entry: Mapping[str, Any], index: int) -> str | None:
    value = entry.get("witness")
    if value is None:
        return None
    if value not in WITNESS_CLASSES:
        raise KaraokeCacheError(
            f"word {index} witness {value!r} is not one of {list(WITNESS_CLASSES)}"
        )
    return str(value)


def _input_word(entry: Mapping[str, Any], index: int) -> KaraokeWord:
    if not isinstance(entry, Mapping):
        raise KaraokeCacheError(f"word {index} must be a mapping, got {type(entry)}")
    unknown = sorted(set(entry) - INPUT_WORD_KEYS)
    if unknown:
        raise KaraokeCacheError(
            f"word {index} carries unsupported keys {unknown}; the artifact "
            f"key set is fixed at {sorted(INPUT_WORD_KEYS)}"
        )
    missing = sorted(REQUIRED_INPUT_WORD_KEYS - set(entry))
    if missing:
        raise KaraokeCacheError(f"word {index} is missing required keys {missing}")
    text = entry["word"]
    if not isinstance(text, str) or not text:
        raise KaraokeCacheError(f"word {index} must be a non-empty string, got {text!r}")
    line_final = entry["line_final"]
    if not isinstance(line_final, bool):
        raise KaraokeCacheError(
            f"word {index} line_final must be a bool, got {line_final!r}"
        )
    numbers = {key: _input_number(entry, key, index) for key in _NULLABLE_NUMBER_KEYS}
    return KaraokeWord(
        idx=index,
        word=text,
        witness=_input_witness(entry, index),
        line_final=line_final,
        **numbers,
    )


def build_words(
    *, stable_id: str, source: str, words: Sequence[Mapping[str, Any]],
    pipeline_version: str = PIPELINE_VERSION,
) -> KaraokeWords:
    """Validate caller word dicts into a :class:`KaraokeWords`, assigning idx.

    ``pipeline_version`` defaults to the module constant because that IS the
    version this code produces; a caller may only override it to re-serialise
    words that a NAMED earlier version produced (the legacy migration does not
    do this, so today the default is the only value in use).
    """
    if not stable_id:
        raise KaraokeCacheError("stable_id must be a non-empty string")
    if not isinstance(source, str) or not source:
        raise KaraokeCacheError(
            f"source must be a non-empty string for {stable_id!r}, got {source!r}"
        )
    if not isinstance(pipeline_version, str) or not pipeline_version:
        raise KaraokeCacheError("pipeline_version must be a non-empty string")
    return KaraokeWords(
        stable_id=stable_id,
        source=source,
        pipeline_version=pipeline_version,
        words=tuple(_input_word(entry, index) for index, entry in enumerate(words)),
    )


#-----------------------------------------------------------------------------
# writing
#-----------------------------------------------------------------------------
def payload_of(words: KaraokeWords) -> dict[str, Any]:
    """The exact JSON object an artifact holds. Fixed key set, both levels."""
    return {
        "schema": KARAOKE_CACHE_SCHEMA,
        "stable_id": words.stable_id,
        "source": words.source,
        "pipeline_version": words.pipeline_version,
        "words": [
            {
                "idx": word.idx,
                "word": word.word,
                "start_s": word.start_s,
                "end_s": word.end_s,
                "score": word.score,
                "witness": word.witness,
                "asr_delta_s": word.asr_delta_s,
                "line_final": word.line_final,
            }
            for word in words.words
        ],
    }


def canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    """The ONE serialisation ``words_content_hash`` is taken over."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def write_words(path: Path, words: KaraokeWords) -> tuple[Path, str, int, int]:
    """Write the whole word list atomically. Returns (path, sha256, n_words, n_lines).

    ``n_lines`` comes from :func:`apps.lyrics.lines.derive_lines`, the one
    canonical grouping, so the stored count and what any surface renders can
    never disagree.
    """
    body = canonical_bytes(payload_of(words))
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temporary.write(body)
        temporary_path = Path(temporary.name)
    try:
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return (
        path,
        hashlib.sha256(body).hexdigest(),
        len(words.words),
        len(lines.derive_lines(words.words)),
    )


#-----------------------------------------------------------------------------
# parsing
#-----------------------------------------------------------------------------
def _parsed_number(entry: Mapping[str, Any], field: str, index: int) -> float | None:
    value = entry[field]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise KaraokeCacheError(
            f"karaoke-words word {index} field {field} must be a number or null"
        )
    return float(value)


def _parsed_word(value: Any, index: int) -> KaraokeWord:
    if not isinstance(value, dict):
        raise KaraokeCacheError(f"karaoke-words word {index} must be a JSON object")
    if set(value) != WORD_KEYS:
        raise KaraokeCacheError(
            f"karaoke-words word {index} keys {sorted(value)} are not exactly "
            f"{sorted(WORD_KEYS)}"
        )
    idx = value["idx"]
    if isinstance(idx, bool) or not isinstance(idx, int) or idx != index:
        raise KaraokeCacheError(
            f"karaoke-words word at position {index} carries idx {idx!r}; "
            "indices must run contiguously from 0"
        )
    text = value["word"]
    if not isinstance(text, str) or not text:
        raise KaraokeCacheError(f"karaoke-words word {index} must be a non-empty string")
    witness = value["witness"]
    if witness is not None and witness not in WITNESS_CLASSES:
        raise KaraokeCacheError(
            f"karaoke-words word {index} witness {witness!r} is not one of "
            f"{list(WITNESS_CLASSES)}"
        )
    line_final = value["line_final"]
    if not isinstance(line_final, bool):
        raise KaraokeCacheError(f"karaoke-words word {index} line_final must be a bool")
    numbers = {key: _parsed_number(value, key, index) for key in _NULLABLE_NUMBER_KEYS}
    return KaraokeWord(
        idx=index, word=text, witness=witness, line_final=line_final, **numbers
    )


def _parsed_string(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise KaraokeCacheError(f"karaoke-words artifact has invalid {field}")
    return value


def parse_words(source: bytes | Path, expected_stable_id: str) -> KaraokeWords:
    """Parse artifact bytes (or a file) strictly, for ONE expected track."""
    body = source.read_bytes() if isinstance(source, Path) else source
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise KaraokeCacheError("karaoke-words artifact must be a JSON object")
    if set(payload) != {"schema", "stable_id", "source", "pipeline_version", "words"}:
        raise KaraokeCacheError(
            f"karaoke-words artifact keys {sorted(payload)} are not exactly "
            "['pipeline_version', 'schema', 'source', 'stable_id', 'words']"
        )
    if payload["schema"] != KARAOKE_CACHE_SCHEMA:
        raise KaraokeCacheError(
            f"karaoke-words artifact has unsupported schema {payload['schema']!r}"
        )
    stable_id = _parsed_string(payload, "stable_id")
    if stable_id != expected_stable_id:
        raise KaraokeCacheError(
            f"karaoke-words artifact is for {stable_id!r}, not {expected_stable_id!r}"
        )
    words = payload["words"]
    if not isinstance(words, list):
        raise KaraokeCacheError("karaoke-words artifact words must be a JSON array")
    return KaraokeWords(
        stable_id=stable_id,
        source=_parsed_string(payload, "source"),
        pipeline_version=_parsed_string(payload, "pipeline_version"),
        words=tuple(_parsed_word(word, index) for index, word in enumerate(words)),
    )


__all__ = [
    "INPUT_WORD_KEYS",
    "KARAOKE_CACHE_DIRNAME",
    "KARAOKE_CACHE_SCHEMA",
    "PIPELINE_VERSION",
    "REQUIRED_INPUT_WORD_KEYS",
    "WITNESS_CLASSES",
    "WORD_KEYS",
    "KaraokeCacheError",
    "KaraokeWord",
    "KaraokeWords",
    "build_words",
    "cache_dir",
    "cache_path",
    "canonical_bytes",
    "parse_words",
    "payload_of",
    "write_words",
]
