"""Ambiguous relink tie-breaker scoring and review CSV.

Split from ``tests/reconcile/test_relink.py`` for the #1390 file-size gate.

[if] tie-breakers resolve on taste alone [then] ambiguous relinks apply wrongly, [else stop].
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from apps.reconcile import index_disk, match, tiebreak
from tests.reconcile.relink_support import (
    _audio,
    _cand,
    _cli_data_dir,
    _disk_entry,
    _result,
    _sha256,
)

pytestmark = pytest.mark.requirement("RECON-02")

# ----- tie-breakers -----------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/Users/user/Music/Manual Library/x.mp3", "curated"),
        ("/Users/user/Music/Media.localized/Music/a/b/x.mp3", "curated"),
        ("/Users/user/Documents/Convert temp/TODO/x.mp3", "staging"),
        ("/Users/user/Music/Manual Library/Sep+ copy/x.mp3", "staging"),
        ("/Users/user/Downloads/x.mp3", "staging"),
        ("/Users/user/Music/Bass, House, Dance/x.mp3", "neutral"),
        # A curated-looking folder under the system temp dir is still staging:
        # an ancestor staging segment outranks the leaf, by design.
        ("/tmp/pytest-of-runner/pytest-0/t0/Manual Library/x.mp3", "staging"),
    ],
)
def test_path_classes_come_from_the_real_folder_names(path: str, expected: str) -> None:
    assert tiebreak.classify_path(path) == expected


def test_ambient_temp_neutral_keeps_the_folder_name_rules(
    tmp_path: Path,
    ambient_temp_neutral: None,
) -> None:
    """if the neutral-ancestry fixture mutes folder-name rules then it hides bugs"""
    # Relative candidate strings must classify by exactly the folder names the
    # test spells out, with the full production vocabulary live. If the fixture
    # ever went back to editing STAGING_SEGMENTS, the second assertion here is
    # the one that would catch a lost folder-name rule.
    assert tiebreak.classify_path("x.mp3") == "neutral"
    assert tiebreak.classify_path("Manual Library/x.mp3") == "curated"
    assert tiebreak.classify_path("Convert temp/x.mp3") == "staging"
    # And the production globals really are untouched.
    assert "/tmp/" in tiebreak.STAGING_SEGMENTS
    assert "temp/" in tiebreak.STAGING_SEGMENTS


def test_tmp_staging_rule_is_intact_without_the_neutraliser() -> None:
    """if "/tmp/" stops meaning staging then a real temp-dir copy outranks a library"""
    # The rule the neutral-ancestry fixture routes AROUND (via relative
    # paths). Deleting "/tmp/" from STAGING_SEGMENTS to make fixtures pass
    # would break real classification, so pin it here, outside the fixture.
    assert tiebreak.classify_path("/tmp/pytest-of-runner/p0/Manual Library/x.mp3") == "staging"


def test_duration_agreement_ranks_a_candidate_first(tmp_path: Path) -> None:
    right = _audio(tmp_path, "dup.mp3")
    wrong = _audio(tmp_path / "other", "dup.mp3")
    index = index_disk.DiskIndex.build(
        [_disk_entry(str(right), duration=232.0), _disk_entry(str(wrong), duration=95.0)]
    )
    row = match.TrackRow(
        stable_id="a",
        title="Quiet Rooms",
        artist="Orla Finch",
        isrc=None,
        duration_ms=232000,
        file_path="/gone/dup.mp3",
    )
    res = _result(
        "a",
        bucket="relinkable-ambiguous",
        path="/gone/dup.mp3",
        candidates=[_cand(str(wrong), 0.90, "basename-duration"), _cand(str(right), 0.90, "basename-duration")],
        ambiguity="multiple-above-threshold",
    )
    scored = tiebreak.score_candidates(row, res, index)
    assert scored.candidates[0].path == str(right), (
        "if duration agreement does not rank first then the tie-breaker adds no information over the matcher"
    )
    assert scored.candidates[0].duration_signal == tiebreak.W_DURATION_TIGHT


def test_contradicted_isrc_ranks_below_a_candidate_with_no_isrc(tmp_path: Path) -> None:
    liar = _audio(tmp_path, "a.mp3")
    quiet = _audio(tmp_path / "other", "a.mp3")
    index = index_disk.DiskIndex.build([_disk_entry(str(liar), isrc="ZZZZZ0000000"), _disk_entry(str(quiet))])
    row = match.TrackRow(
        stable_id="a",
        title="T",
        artist="A",
        isrc="GBAYE2101234",
        duration_ms=None,
        file_path="/gone/a.mp3",
    )
    res = _result(
        "a",
        bucket="relinkable-ambiguous",
        path="/gone/a.mp3",
        candidates=[_cand(str(liar), 0.90), _cand(str(quiet), 0.90)],
        ambiguity="multiple-above-threshold",
    )
    scored = tiebreak.score_candidates(row, res, index)
    assert scored.candidates[0].path == str(quiet)
    assert scored.candidates[-1].isrc_signal == tiebreak.W_ISRC_CONTRADICTED


def test_folder_class_alone_never_resolves_a_row(
    tmp_path: Path,
    ambient_temp_neutral: None,
) -> None:
    # Real files, addressed RELATIVE to tmp_path (the fixture's cwd) so the
    # classified string carries only the folder names this test spells out.
    _audio(tmp_path / "Manual Library", "dup.mp3")
    _audio(tmp_path / "Convert temp", "dup.mp3")
    curated = "Manual Library/dup.mp3"
    staging = "Convert temp/dup.mp3"
    index = index_disk.DiskIndex.build([_disk_entry(curated, duration=232.0), _disk_entry(staging, duration=232.0)])
    row = match.TrackRow(
        stable_id="a",
        title="T",
        artist="A",
        isrc=None,
        duration_ms=232000,
        file_path="/gone/dup.mp3",
    )
    res = _result(
        "a",
        bucket="relinkable-ambiguous",
        path="/gone/dup.mp3",
        candidates=[_cand(curated, 0.90, "basename-duration"), _cand(staging, 0.90, "basename-duration")],
        ambiguity="multiple-above-threshold",
    )
    scored = tiebreak.score_rows([row], [res], index)[0]
    # Both candidates must SURVIVE the hard filters, otherwise _verdict
    # short-circuits on "no candidate survives" and never reaches the margin
    # rule this test exists to pin. Assert that explicitly rather than trust it.
    assert [c.hard_reject for c in scored.candidates] == ["", ""]
    assert scored.candidates[0].path == curated
    assert not scored.resolved, (
        "if folder plausibility alone resolves a row then two identical-duration "
        "files can be separated on taste and applied"
    )
    assert scored.resolved_reason and "under" in scored.resolved_reason
    assert tiebreak.RESOLVE_MARGIN > tiebreak.PATH_SPAN


def test_content_signals_resolve_a_single_weak_candidate(tmp_path: Path) -> None:
    only = _audio(tmp_path / "Manual Library", "renamed.mp3")
    index = index_disk.DiskIndex.build([_disk_entry(str(only), duration=232.0, size=4242, isrc="GBAYE2101234")])
    row = match.TrackRow(
        stable_id="a",
        title="Quiet Rooms",
        artist="Orla Finch",
        isrc="GBAYE2101234",
        duration_ms=232000,
        file_path="/gone/02 Quiet Rooms.mp3",
        expected_size_bytes=4242,
    )
    res = _result(
        "a",
        bucket="relinkable-ambiguous",
        path="/gone/02 Quiet Rooms.mp3",
        candidates=[_cand(str(only), 0.78, "title-artist-fuzzy")],
        ambiguity="below-auto-threshold",
    )
    scored = tiebreak.score_rows([row], [res], index)[0]
    assert scored.resolved, (
        "if duration + size + ISRC agreement cannot resolve a lone fuzzy "
        "candidate then the tie-breakers can never recover anything"
    )
    assert scored.top is not None and scored.top.score >= tiebreak.RESOLVE_SCORE_MIN


def test_target_already_linked_is_never_resolved(tmp_path: Path) -> None:
    taken = _audio(tmp_path, "taken.mp3")
    index = index_disk.DiskIndex.build([_disk_entry(str(taken), duration=232.0, size=1, isrc="GBAYE2101234")])
    row = match.TrackRow(
        stable_id="a",
        title="T",
        artist="A",
        isrc="GBAYE2101234",
        duration_ms=232000,
        file_path="/gone/taken.mp3",
        expected_size_bytes=1,
    )
    res = _result(
        "a",
        bucket="relinkable-ambiguous",
        path="/gone/taken.mp3",
        candidates=[_cand(str(taken), 0.95)],
        ambiguity="target-already-linked",
    )
    other = _result("b", bucket="present", path=str(taken))
    scored = tiebreak.score_rows([row, match.TrackRow("b", None, None, None, None, str(taken))], [res, other], index)[0]
    assert not scored.resolved
    assert "duplicate decision" in scored.resolved_reason
    assert scored.top is None, "if a taken path is still rank 1 then a reviewer can accept a duplicate"


def test_a_contest_is_awarded_to_the_best_scoring_claimant(tmp_path: Path) -> None:
    target = _audio(tmp_path / "Manual Library", "shared.mp3")
    index = index_disk.DiskIndex.build([_disk_entry(str(target), duration=232.0, size=4242, isrc="GBAYE2101234")])
    winner = match.TrackRow(
        stable_id="win",
        title="T",
        artist="A",
        isrc="GBAYE2101234",
        duration_ms=232000,
        file_path="/gone/one/shared.mp3",
        expected_size_bytes=4242,
    )
    loser = match.TrackRow(
        stable_id="lose",
        title="T",
        artist="A",
        isrc=None,
        duration_ms=900000,
        file_path="/gone/two/shared.mp3",
    )
    results = [
        _result(
            "win",
            bucket="relinkable-ambiguous",
            path="/gone/one/shared.mp3",
            candidates=[_cand(str(target), 0.95)],
            ambiguity="target-contested",
        ),
        _result(
            "lose",
            bucket="relinkable-ambiguous",
            path="/gone/two/shared.mp3",
            candidates=[_cand(str(target), 0.95)],
            ambiguity="target-contested",
        ),
    ]
    scored = tiebreak.score_rows([winner, loser], results, index)
    by_id = {r.stable_id: r for r in scored}
    assert by_id["win"].resolved and not by_id["lose"].resolved, (
        "if a contest cannot be awarded then 22 real rows stay stuck forever"
    )
    assert "wins a 2-row contest" in by_id["win"].resolved_reason


def test_review_csv_ranks_candidates_and_shares_one_verdict(
    tmp_path: Path,
    ambient_temp_neutral: None,
) -> None:
    _audio(tmp_path / "Manual Library", "dup.mp3")
    _audio(tmp_path / "Convert temp", "dup.mp3")
    a = "Manual Library/dup.mp3"
    b = "Convert temp/dup.mp3"
    index = index_disk.DiskIndex.build([_disk_entry(a, duration=232.0), _disk_entry(b, duration=232.0)])
    row = match.TrackRow("a", "T", "A", None, 232000, "/gone/dup.mp3")
    res = _result(
        "a",
        bucket="relinkable-ambiguous",
        path="/gone/dup.mp3",
        candidates=[_cand(a, 0.90, "basename-duration"), _cand(b, 0.90, "basename-duration")],
        ambiguity="multiple-above-threshold",
    )
    scored = tiebreak.score_rows([row], [res], index)
    out = tiebreak.write_review_csv(scored, tiebreak.review_path_for(tmp_path, date="2026-07-28"))
    assert out.name == "relink-review-2026-07-28.csv"
    lines = list(csv.DictReader(out.open(encoding="utf-8")))
    assert [int(line["rank"]) for line in lines] == [1, 2]
    assert {line["resolved"] for line in lines} == {"no"}
    # "no" must mean "the margin rule refused it", not "both candidates were
    # hard-rejected as missing" -- the latter passes this assertion for the
    # wrong reason and makes the whole CSV unrepresentative of a real review.
    assert {line["resolved_reason"] for line in lines} == {
        line["resolved_reason"] for line in lines if "under" in line["resolved_reason"]
    }
    assert lines[0]["path_class"] == "curated" and lines[1]["path_class"] == "staging"
    assert set(lines[0]) == set(tiebreak.REVIEW_COLUMNS)


def test_review_cli_leaves_state_db_untouched(tmp_path: Path) -> None:
    data_dir, disk, _ = _cli_data_dir(tmp_path)
    db = data_dir / "state" / "state.db"
    digest = _sha256(db)
    code = tiebreak.main(
        [
            "--data-dir",
            str(data_dir),
            "--roots",
            str(disk),
            "--index-cache",
            str(tmp_path / "c.json"),
            "--out",
            str(tmp_path / "review.csv"),
        ]
    )
    assert code == 0 and _sha256(db) == digest
