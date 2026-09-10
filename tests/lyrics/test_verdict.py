"""Regression tests for apps/lyrics/verdict.py (pure, synthetic fixtures).

- if a clean sheet stops classifying as matched then broken
- if a dropped-verse sheet stops surfacing extra_in_audio then broken
- if a doubled-chorus sheet stops surfacing sheet_repeat_unsupported then broken
- if a moved block stops pairing into moved_block then broken
- if disjoint texts stop classifying wrong_song, or a near-empty ASR stream
  stops classifying unverifiable, then broken
"""

from __future__ import annotations

from apps.lyrics.verdict import classify

VERSE_A = ["walking", "down", "the", "empty", "street", "under", "silver", "light", "tonight"]
VERSE_B = ["morning", "comes", "with", "golden", "rays", "across", "the", "sleeping", "town"]
CHORUS = ["fire", "in", "the", "night", "burning", "ever", "bright", "hold", "on", "tight"]
SONG = VERSE_A + CHORUS + VERSE_B + CHORUS


def test_clean_sheet_is_matched() -> None:
    v = classify(SONG, SONG)
    assert v.verdict == "matched", "if identical streams stop matching then broken"
    assert v.findings == (), "if a clean match invents findings then broken"
    assert v.ref_match_rate == 1.0


def test_dropped_verse_surfaces_extra_in_audio() -> None:
    sheet_missing_verse_b = VERSE_A + CHORUS + CHORUS
    v = classify(sheet_missing_verse_b, SONG)
    assert v.verdict == "structure_mismatch"
    kinds = [f.kind for f in v.findings]
    assert "extra_in_audio" in kinds, "if the audio's unlisted verse is not flagged then broken"
    (f,) = [f for f in v.findings if f.kind == "extra_in_audio"]
    assert f.n_words == len(VERSE_B), "if the flagged block is not the dropped verse then broken"


def test_doubled_chorus_needs_acoustic_check() -> None:
    sheet_extra_chorus = VERSE_A + CHORUS + CHORUS + VERSE_B + CHORUS
    v = classify(sheet_extra_chorus, SONG)
    assert v.verdict == "needs_acoustic_check", (
        "if a repeat-count dispute is judged from text alone then broken -- "
        "'ASR missed a repeat' and 'sheet invented a repeat' read identically"
    )
    kinds = [f.kind for f in v.findings]
    assert "sheet_repeat_unsupported" in kinds, (
        "if the sheet's phantom chorus repeat is not flagged as a repeat then broken"
    )


def test_garbled_section_rescued_with_probs() -> None:
    # ASR heard VERSE_B badly: 4 of 9 words wrong, low confidence on the garble.
    garbled = ["morning", "comes", "with", "gxx", "ryy", "azz", "the", "sleeping", "town"]
    asr = VERSE_A + CHORUS + garbled + CHORUS
    probs = [0.9] * (len(VERSE_A) + len(CHORUS)) + [0.2] * len(garbled) + [0.9] * len(CHORUS)
    v = classify(SONG, asr, asr_probs=probs)
    assert v.verdict == "matched", (
        "if a heard-but-garbled verse still flags a structure mismatch then broken"
    )


def test_moved_block_pairs() -> None:
    sheet_moved = CHORUS + VERSE_A + VERSE_B + CHORUS  # first chorus moved to front
    v = classify(sheet_moved, SONG)
    assert v.verdict == "structure_mismatch"
    kinds = [f.kind for f in v.findings]
    assert "moved_block" in kinds, "if the relocated chorus does not pair into a move then broken"


def test_wrong_song_and_unverifiable() -> None:
    other = ["completely", "different", "words", "about", "boats", "and", "rivers",
             "flowing", "somewhere", "else", "entirely", "tonight", "again", "forever"]
    v = classify(SONG, other * 3)
    assert v.verdict == "wrong_song", "if disjoint texts stop reading wrong_song then broken"
    v2 = classify(SONG, ["fire", "night"])
    assert v2.verdict == "unverifiable", "if a near-empty ASR stream is judged then broken"


def test_audio_extra_chorus_surfaces_audio_repeat_unlisted() -> None:
    sheet_missing_second_chorus = VERSE_A + CHORUS + VERSE_B  # audio sings CHORUS again at end
    v = classify(sheet_missing_second_chorus, SONG)
    assert v.verdict == "structure_mismatch", (
        "if an audio-side extra repeat is not decisive then broken (ASR positively heard it)"
    )
    kinds = [f.kind for f in v.findings]
    assert "audio_repeat_unlisted" in kinds, (
        "if the audio's unlisted chorus repeat is not classified directionally then broken"
    )
