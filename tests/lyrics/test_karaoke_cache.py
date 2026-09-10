"""The karaoke_words artifact contract: canonical bytes and a strict parser.

Everything the retired ``lyric_word`` DDL enforced (three CHECKs, a NOT NULL,
a composite PK's uniqueness and contiguity) is now the parser's job, so these
are the assertions that keep "the database refused it" from quietly becoming
"the file was written".

- if the canonical bytes gain a trailing newline, indentation or escaped
  non-ASCII then every words_content_hash in the library changes and no row
  can be verified against its artifact -- broken
- if the writer reads idx from the caller instead of assigning it positionally
  then a manifest with a stale index silently mis-times the back half -- broken
- if the parser accepts an unknown key, a bad witness, a non-contiguous idx,
  the wrong schema or another track's stable_id then the artifact is no longer
  a contract -- broken
- if n_lines stops agreeing with lines.derive_lines then the stored column and
  what the UI renders disagree -- broken
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from apps.lyrics import karaoke_cache, lines
from apps.lyrics.karaoke_cache import KaraokeCacheError

from .conftest import word

WORDS = [
    word("hello", start_s=1.0, end_s=1.2, score=-0.5, witness="agree"),
    word("wörld", start_s=1.3, end_s=1.9, witness="contradict", line_final=True),
    word("again", asr_delta_s=0.25),
]


def _build(stable_id: str = "sid-1", source: str = "lrclib get"):
    return karaoke_cache.build_words(stable_id=stable_id, source=source, words=WORDS)


def test_pipeline_version_follows_its_grammar() -> None:
    """<YYYY.MM.DD>-<round>: a row must be traceable to the run that made it."""
    assert re.fullmatch(r"\d{4}\.\d{2}\.\d{2}-[a-z0-9]+", karaoke_cache.PIPELINE_VERSION)


def test_canonical_bytes_are_sorted_compact_and_newline_free() -> None:
    body = karaoke_cache.canonical_bytes(karaoke_cache.payload_of(_build()))
    assert not body.endswith(b"\n")
    assert b", " not in body and b": " not in body
    assert "wörld".encode() in body, "ensure_ascii=False keeps real UTF-8"
    keys = list(json.loads(body))
    assert keys == sorted(keys), "sort_keys=True is part of the hash contract"


def test_write_returns_the_hash_of_the_file_on_disk(tmp_path: Path) -> None:
    path, digest, n_words, n_lines = karaoke_cache.write_words(
        karaoke_cache.cache_path(tmp_path, "sid-1"), _build()
    )
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert (n_words, n_lines) == (3, 2)


def test_n_lines_agrees_with_derive_lines(tmp_path: Path) -> None:
    built = _build()
    _, _, _, n_lines = karaoke_cache.write_words(
        karaoke_cache.cache_path(tmp_path, "sid-1"), built
    )
    assert n_lines == len(lines.derive_lines(built.words))


def test_idx_is_assigned_positionally_not_read(tmp_path: Path) -> None:
    """The whole-list rule: the caller never supplies an index."""
    with pytest.raises(KaraokeCacheError, match="unsupported keys"):
        karaoke_cache.build_words(
            stable_id="sid-1", source="lrclib get", words=[{**word("a"), "idx": 7}]
        )
    built = _build()
    assert [entry.idx for entry in built.words] == [0, 1, 2]


@pytest.mark.parametrize(
    "entry, match",
    [
        ({**word("a"), "unexpected": 1}, "unsupported keys"),
        ({"word": "a"}, "missing required keys"),
        ({**word(""), }, "non-empty string"),
        ({**word("a"), "witness": "nope"}, "witness"),
        ({**word("a"), "line_final": 1}, "line_final must be a bool"),
        ({**word("a"), "start_s": "1.0"}, "must be a number or null"),
    ],
)
def test_builder_refuses_bad_input(entry: dict, match: str) -> None:
    with pytest.raises(KaraokeCacheError, match=match):
        karaoke_cache.build_words(stable_id="sid-1", source="lrclib get", words=[entry])


def test_builder_requires_a_source() -> None:
    """The artifact is self-describing; a nameless provider cannot be purged."""
    with pytest.raises(KaraokeCacheError, match="source must be a non-empty string"):
        karaoke_cache.build_words(stable_id="sid-1", source="", words=[word("a")])


def test_cache_path_refuses_a_traversing_stable_id(tmp_path: Path) -> None:
    with pytest.raises(KaraokeCacheError, match="escapes karaoke-cache"):
        karaoke_cache.cache_path(tmp_path, "../../etc/passwd")


def test_parse_round_trips_every_field(tmp_path: Path) -> None:
    path, _, _, _ = karaoke_cache.write_words(
        karaoke_cache.cache_path(tmp_path, "sid-1"), _build()
    )
    parsed = karaoke_cache.parse_words(path, "sid-1")
    assert parsed.source == "lrclib get"
    assert parsed.pipeline_version == karaoke_cache.PIPELINE_VERSION
    assert [entry.word for entry in parsed.words] == ["hello", "wörld", "again"]
    assert parsed.words[0].score == -0.5
    assert parsed.words[2].asr_delta_s == 0.25, "asr_delta_s must survive the boundary"
    assert parsed.words[1].line_final is True


def _payload(**overrides) -> bytes:
    payload = karaoke_cache.payload_of(_build())
    payload.update(overrides)
    return karaoke_cache.canonical_bytes(payload)


def test_parse_refuses_the_wrong_track() -> None:
    with pytest.raises(KaraokeCacheError, match="is for 'sid-1', not 'other'"):
        karaoke_cache.parse_words(_payload(), "other")


def test_parse_refuses_an_unknown_schema() -> None:
    with pytest.raises(KaraokeCacheError, match="unsupported schema"):
        karaoke_cache.parse_words(_payload(schema=2), "sid-1")


def test_parse_refuses_an_unknown_envelope_key() -> None:
    with pytest.raises(KaraokeCacheError, match="are not exactly"):
        karaoke_cache.parse_words(_payload(extra="x"), "sid-1")


def test_parse_refuses_non_contiguous_indices() -> None:
    payload = karaoke_cache.payload_of(_build())
    payload["words"][1]["idx"] = 5
    with pytest.raises(KaraokeCacheError, match="contiguously from 0"):
        karaoke_cache.parse_words(karaoke_cache.canonical_bytes(payload), "sid-1")


def test_parse_refuses_an_out_of_vocabulary_witness() -> None:
    payload = karaoke_cache.payload_of(_build())
    payload["words"][0]["witness"] = "maybe"
    with pytest.raises(KaraokeCacheError, match="witness 'maybe'"):
        karaoke_cache.parse_words(karaoke_cache.canonical_bytes(payload), "sid-1")


def test_parse_refuses_a_word_missing_a_key() -> None:
    payload = karaoke_cache.payload_of(_build())
    del payload["words"][0]["asr_delta_s"]
    with pytest.raises(KaraokeCacheError, match="are not exactly"):
        karaoke_cache.parse_words(karaoke_cache.canonical_bytes(payload), "sid-1")


def test_write_is_atomic_and_leaves_no_temp_file(tmp_path: Path) -> None:
    path = karaoke_cache.cache_path(tmp_path, "sid-1")
    karaoke_cache.write_words(path, _build())
    karaoke_cache.write_words(path, _build())
    assert sorted(entry.name for entry in path.parent.iterdir()) == ["sid-1.json"]
