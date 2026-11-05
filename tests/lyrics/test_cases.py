"""Regression tests for apps/lyrics/cases.py (pure synthetic fixtures).

- if build_cases drifts case names, verdict labels, or perturb wiring then broken
- if the crc32 seed or wrong_song peer rotation changes then broken
"""

from __future__ import annotations

from pathlib import Path

from apps.lyrics.cases import build_cases
from apps.lyrics.jamendo import JamendoSong, WordSpan

_CHORUS_LINES = [
    ["intro", "one"],
    ["verse", "line", "two"],
    ["fire", "fly"],
    ["burn", "high"],
    ["verse", "three"],
    ["verse", "four"],
    ["fire", "fly"],
    ["burn", "high"],
]
_CHORUS_WORDS = [w for line in _CHORUS_LINES for w in line]
_CHORUS_FINALS = [
    i == len(line) - 1 for line in _CHORUS_LINES for i in range(len(line))
]


def _song(name: str, words: list[str], finals: list[bool], language: str = "English") -> JamendoSong:
    spans = tuple(
        WordSpan(
            word=word,
            start_s=index * 0.5,
            end_s=(index + 1) * 0.5 if final else None,
            line_final=final,
        )
        for index, (word, final) in enumerate(zip(words, finals, strict=True))
    )
    return JamendoSong(
        name=name,
        artist="fixture",
        title=name,
        language=language,
        lyric_overlap=True,
        polyphonic=False,
        non_lexical=False,
        mp3_path=Path("/fixtures/lyrics/fixture.mp3"),
        words=spans,
    )


def test_build_cases_includes_chorus_perturbations_when_repeat_present() -> None:
    song = _song("chorus-fixture", _CHORUS_WORDS, _CHORUS_FINALS)
    peers = [song, _song("other-en", ["a", "b"], [True, True])]
    cases = build_cases(song, peers)
    names = [c.case for c in cases]
    assert names == [
        "none",
        "wrong_song",
        "drop_block",
        "move_block",
        "sheet_extra_chorus",
        "sheet_prepend_hook",
        "audio_extra_chorus",
    ], "if chorus-aware case list drifts then broken"
    prepend = next(c for c in cases if c.case == "sheet_prepend_hook")
    extra = next(c for c in cases if c.case == "sheet_extra_chorus")
    assert prepend.target_words == extra.target_words, (
        "if prepend_hook stops reusing the chorus block target then broken"
    )


def test_build_cases_wrong_song_uses_next_language_peer() -> None:
    first = _song("chorus-fixture", _CHORUS_WORDS, _CHORUS_FINALS)
    second = _song("peer-b", ["two", "three"], [False, True])
    wrong = next(c for c in build_cases(first, [first, second]) if c.case == "wrong_song")
    assert wrong.sheet_words == ["two", "three"], "if wrong_song peer rotation drifts then broken"
    assert wrong.wrong_song_name == "peer-b"


def test_build_cases_none_is_identity_sheet() -> None:
    song = _song("chorus-fixture", _CHORUS_WORDS, _CHORUS_FINALS)
    peers = [song, _song("peer-b", ["z"], [True])]
    none_case = next(c for c in build_cases(song, peers) if c.case == "none")
    assert none_case.sheet_words == _CHORUS_WORDS
    assert none_case.source_idx == list(range(len(_CHORUS_WORDS)))
    assert none_case.expected_verdict == "matched"
