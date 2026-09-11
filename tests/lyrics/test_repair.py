"""Regression tests for apps/lyrics/repair.py (pure, synthetic fixtures).

- if a phantom sheet chorus is not deleted by repair then broken
- if an audio-side extra chorus is not inserted as a source-less copy then broken
- if a repaired sheet does not re-classify as matched (green after repair) then broken
- if unverifiable/wrong_song sheets are repairable then broken
"""

from __future__ import annotations

import pytest

from apps.lyrics.repair import repair_sheet
from apps.lyrics.verdict import Verdict, classify

VERSE_A = ["walking", "down", "the", "empty", "street", "under", "silver", "light", "tonight"]
VERSE_B = ["morning", "comes", "with", "golden", "rays", "across", "the", "sleeping", "town"]
CHORUS = ["fire", "in", "the", "night", "burning", "ever", "bright", "hold", "on", "tight"]
SONG = VERSE_A + CHORUS + VERSE_B + CHORUS  # what the audio sings (ASR heard exactly this)


def test_phantom_chorus_deleted_and_green_after_repair() -> None:
    sheet = VERSE_A + CHORUS + CHORUS + VERSE_B + CHORUS  # sheet claims 3 choruses, audio sings 2
    classified = classify(sheet, SONG)
    assert classified.verdict == "needs_acoustic_check"
    assert any(f.kind == "sheet_repeat_unsupported" for f in classified.findings)
    v = Verdict(
        verdict="structure_mismatch",
        ref_match_rate=classified.ref_match_rate,
        asr_match_rate=classified.asr_match_rate,
        longest_anchor=classified.longest_anchor,
        findings=classified.findings,
    )
    r = repair_sheet(sheet, SONG, v)
    assert len(r.words) == len(SONG), "if the phantom chorus survives repair then broken"
    v2 = classify(r.words, SONG)
    assert v2.verdict == "matched", "if repair does not come back green then broken"
    assert r.asr_sourced_spans == 0


def test_audio_extra_chorus_inserted_as_sourceless_copy() -> None:
    sheet = VERSE_A + CHORUS + VERSE_B  # audio sings a final chorus the sheet lacks
    v = classify(sheet, SONG)
    assert any(f.kind == "audio_repeat_unlisted" for f in v.findings)
    r = repair_sheet(sheet, SONG, v)
    assert len(r.words) == len(SONG), "if the missing repeat is not inserted then broken"
    inserted = [i for i, s in enumerate(r.source_idx) if s is None]
    assert [r.words[i] for i in inserted] == CHORUS, (
        "if the insert is not the chorus copy then broken"
    )
    assert classify(r.words, SONG).verdict == "matched", (
        "if repair does not come back green then broken"
    )


def test_dropped_verse_reinserted_from_asr() -> None:
    sheet = VERSE_A + CHORUS + CHORUS  # sheet lost VERSE_B entirely
    v = classify(sheet, SONG)
    assert any(f.kind == "extra_in_audio" for f in v.findings)
    r = repair_sheet(sheet, SONG, v)
    assert r.asr_sourced_spans == 1, "if the ASR-sourced insert is not flagged then broken"
    assert classify(r.words, SONG).verdict == "matched", (
        "if repair does not come back green then broken"
    )


def test_refuses_unverifiable_and_wrong_song() -> None:
    v = classify(SONG, ["fire", "night"])  # unverifiable
    with pytest.raises(ValueError, match="refusing to repair"):
        repair_sheet(SONG, ["fire", "night"], v)
    clean = classify(SONG, SONG)
    with pytest.raises(ValueError, match="nothing to repair"):
        repair_sheet(SONG, SONG, clean)


def test_refuses_needs_acoustic_check() -> None:
    sheet = VERSE_A + CHORUS + CHORUS + VERSE_B + CHORUS
    v = classify(sheet, SONG)
    assert v.verdict == "needs_acoustic_check"
    with pytest.raises(ValueError, match="refusing to repair"):
        repair_sheet(sheet, SONG, v)
