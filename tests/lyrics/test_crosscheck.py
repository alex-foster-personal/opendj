"""Regression tests for apps/lyrics/crosscheck.py (pure, no dataset needed).

- if matched words do not carry the exact aligner-minus-asr delta then broken
- if unmatched lyric words or over-threshold deltas are not flagged then broken
- if punctuation-only tokens enter match_ratio's denominator or get flagged then broken
- if repeated tokens are junked (autojunk) so a chorus stops matching then broken
"""

from __future__ import annotations

import pytest

from apps.lyrics.crosscheck import crosscheck, flagged_indices, normalize_token


def _asr(*pairs: tuple[str, float]) -> list[dict]:
    return [{"word": w, "start_s": s, "end_s": s + 0.2, "prob": 0.9} for w, s in pairs]


def test_normalize_token() -> None:
    assert normalize_token(" Hey,") == "hey", "if edge punctuation survives then broken"
    assert normalize_token("don’t") == "don't", (  # noqa: RUF001 - curly is the input
        "if curly apostrophes differ then broken"
    )
    assert normalize_token("--") == "", "if bare punctuation normalizes non-empty then broken"


def test_matched_deltas_and_summary() -> None:
    report = crosscheck(
        ["Hey", "now", "go"], [1.0, 2.0, 3.5],
        _asr(("hey", 1.1), ("now", 2.0), ("go", 3.0)),
    )
    statuses = [v.status for v in report.verdicts]
    assert statuses == ["matched", "matched", "matched"], "if clean 1:1 does not match then broken"
    assert report.verdicts[0].delta_s == pytest.approx(-0.1), "if delta != aligner-asr then broken"
    assert report.verdicts[2].delta_s == pytest.approx(0.5)
    assert report.match_ratio == 1.0
    assert report.median_abs_delta_s == pytest.approx(0.1)
    assert flagged_indices(report, 0.4) == {2}, "if >threshold delta is not flagged then broken"


def test_lone_unmatched_word_not_flagged_by_default() -> None:
    report = crosscheck(
        ["one", "mystery", "three"], [1.0, 2.0, 3.0],
        _asr(("one", 1.0), ("three", 3.0)),
    )
    assert report.verdicts[1].status == "unmatched"
    assert report.match_ratio == pytest.approx(2 / 3)
    assert flagged_indices(report, 0.5) == set(), "if a lone asr miss is flagged then broken"
    assert flagged_indices(report, 0.5, min_unmatched_run=1) == {1}, (
        "if run>=1 does not flag the lone unmatched then broken"
    )


def test_unmatched_runs_flag_at_threshold_length() -> None:
    words = ["a"] + ["gone"] * 5 + ["b"]
    starts = [float(i) for i in range(7)]
    report = crosscheck(words, starts, _asr(("a", 0.0), ("b", 6.0)))
    assert flagged_indices(report, 0.5, min_unmatched_run=5) == {1, 2, 3, 4, 5}, (
        "if a 5-run of asr-unheard words is not flagged then broken"
    )
    assert flagged_indices(report, 0.5, min_unmatched_run=6) == set(), (
        "if a 5-run flags at min run 6 then broken"
    )


def test_unmatchable_breaks_an_unmatched_run() -> None:
    words = ["a", "gone", "gone", "--", "gone", "gone", "b"]
    starts = [float(i) for i in range(7)]
    report = crosscheck(words, starts, _asr(("a", 0.0), ("b", 6.0)))
    assert flagged_indices(report, 0.5, min_unmatched_run=4) == set(), (
        "if an unmatchable token does not break the run then broken"
    )


def test_unmatchable_tokens_stay_out_of_denominator_and_flags() -> None:
    report = crosscheck(
        ["hello", "--", "world"], [1.0, 1.5, 2.0],
        _asr(("hello", 1.0), ("world", 2.0)),
    )
    assert report.verdicts[1].status == "unmatchable"
    assert report.n_unmatchable == 1
    assert report.n_matchable == 2
    assert report.match_ratio == 1.0, "if unmatchable enters the denominator then broken"
    assert flagged_indices(report, 0.5) == set(), "if unmatchable gets flagged then broken"


def test_repeated_tokens_survive_autojunk() -> None:
    # 250 repeats of the same token: autojunk=True would junk it and match NOTHING.
    n = 250
    words = ["pa"] * n
    starts = [float(i) for i in range(n)]
    report = crosscheck(words, starts, _asr(*[("pa", float(i)) for i in range(n)]))
    assert report.match_ratio == 1.0, "if repeated tokens get junked then broken"


def test_version_similarity_separates_right_from_wrong_text() -> None:
    right = crosscheck(["a", "b", "c", "d"], [1, 2, 3, 4],
                       _asr(("a", 1), ("b", 2), ("c", 3), ("d", 4)))
    wrong = crosscheck(["w", "x", "y", "z"], [1, 2, 3, 4],
                       _asr(("a", 1), ("b", 2), ("c", 3), ("d", 4)))
    assert right.version_similarity > 0.99
    assert wrong.version_similarity < 0.01, "if wrong text scores similar then broken"


def test_input_validation() -> None:
    with pytest.raises(ValueError, match="1:1"):
        crosscheck(["a"], [1.0, 2.0], _asr(("a", 1.0)))
    with pytest.raises(ValueError, match="no lyric words"):
        crosscheck([], [], _asr(("a", 1.0)))


#-----------------------------------------------------------------------------
# round-5 witness_verdicts (local-first matching)
#-----------------------------------------------------------------------------


def test_witness_local_rescues_diff_orphaned_words() -> None:
    """The Jamming-verse class: ASR heard the words at the right times but the global diff
    bracketed them away. If local same-token evidence 0.1s away still reads 'lost' then broken."""
    from apps.lyrics.crosscheck import witness_verdicts

    # ASR word ORDER scrambled so SequenceMatcher cannot match both, times near-perfect.
    verdicts = witness_verdicts(["hello", "world"], [10.0, 11.0],
                                _asr(("world", 11.1), ("hello", 10.1)))
    assert [v.verdict for v in verdicts] == ["agree", "agree"]
    assert {v.source for v in verdicts} == {"local"}


def test_witness_ambiguity_gate_blocks_dense_tokens() -> None:
    """A token with TWO in-window occurrences proves nothing about which one the aligner meant.
    If ambiguous local evidence yields 'agree' then broken (green error rate regression)."""
    from apps.lyrics.crosscheck import witness_verdicts

    verdicts = witness_verdicts(["la"], [10.0], _asr(("la", 9.2), ("la", 10.8)))
    (v,) = verdicts
    assert v.source == "diff", "ambiguous local evidence must fall through to the global diff"
    assert v.verdict == "drift", "diff matches the first 'la' at 0.8s"


def test_witness_detached_region_stays_red() -> None:
    """A run of 5+ words with no ASR evidence anywhere nearby is a lost region. If a crammed
    chorus reads anything but lost/red then broken."""
    from apps.lyrics.crosscheck import witness_verdicts

    words = ["alpha", "bravo", "charlie", "delta", "echo"]
    verdicts = witness_verdicts(words, [100.0, 100.2, 100.4, 100.6, 100.8],
                                _asr(("zulu", 100.0), ("yankee", 100.5)))
    assert [v.verdict for v in verdicts] == ["lost"] * 5


def test_witness_lone_unheard_is_not_red() -> None:
    from apps.lyrics.crosscheck import witness_verdicts

    verdicts = witness_verdicts(["hello", "qwxz", "world"], [10.0, 10.5, 11.0],
                                _asr(("hello", 10.1), ("world", 11.1)))
    assert [v.verdict for v in verdicts] == ["agree", "unheard", "agree"]


def test_witness_window_zero_reproduces_diff_only() -> None:
    """local_window_s=0 is the round-4 A/B baseline. If it still consults local evidence
    then the ablation flag lies."""
    from apps.lyrics.crosscheck import witness_verdicts

    verdicts = witness_verdicts(["hello", "world"], [10.0, 11.0],
                                _asr(("world", 11.1), ("hello", 10.1)),
                                local_window_s=0.0)
    assert all(v.source in ("diff", "none") for v in verdicts)


def test_witness_far_occurrence_is_contradict_via_diff() -> None:
    """A word whose only ASR occurrence is far outside the window must stay red (the
    uniformly-shifted region class), via the diff fallback."""
    from apps.lyrics.crosscheck import witness_verdicts

    verdicts = witness_verdicts(["hello"], [10.0], _asr(("hello", 30.0)))
    (v,) = verdicts
    assert (v.verdict, v.source) == ("contradict", "diff")
    assert v.delta_s == -20.0
