"""Phase 4 integration smoke test.

Runs the full Phase 4 pipeline (matcher output -> sync_diff -> apply_*
CLIs dry-run mode) against an in-tmp fixture corpus. No live DB writes.

Marked ``@pytest.mark.integration`` so ``make test`` skips it unless
``make integration`` is invoked. Under ``make integration`` it runs
against synthesized CSV inputs and asserts the end-to-end flow
completes without raising.
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from apps.audit.cue_comparison import compare_cue_lists
from apps.audit.sync_diff import build_analysis_diff, write_diff_csv
from apps.shared.normalised import NormalisedAnalysis, NormalisedCue
from apps.sync.apply_analysis import main as analysis_main
from apps.sync.apply_cues import main as cues_main
from apps.sync.apply_ratings import main as ratings_main
from apps.sync.conflict import resolve_cue_array

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requirement("SYNC-04"),
    pytest.mark.requirement("SYNC-05"),
    pytest.mark.requirement("SYNC-06"),
]


def test_runbook_preconditions_on_empty_matches(tmp_path: Path):
    """Precondition check from Plan 2: matches.csv with zero rows -> empty diff."""
    matches = tmp_path / "matches.csv"
    with matches.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.writer(fp)
        writer.writerow(["rb_content_id", "djay_uuid", "confidence"])
    # Build analysis diff from empty inputs -> should not raise.
    analysis, ratings = build_analysis_diff({}, {}, {}, {}, [])
    assert analysis == []
    assert ratings == []


def test_full_phase4_smoke(tmp_path: Path, capsys):
    """Full pipeline smoke: seed -> diff -> apply (dry-run) -> zero writes."""
    # Synthesize a matched pair with known deltas.
    rb_analysis = {
        "1": NormalisedAnalysis(uuid_or_id="1", source="rb", bpm=128.0, key_camelot="8A"),
        "2": NormalisedAnalysis(uuid_or_id="2", source="rb", bpm=140.0),
    }
    djay_analysis = {
        "a": NormalisedAnalysis(uuid_or_id="a", source="djay", bpm=130.0, key_camelot="8A"),
        "b": NormalisedAnalysis(uuid_or_id="b", source="djay", bpm=None, manual_bpm=138.0),
    }
    rb_ratings = {"1": 5, "2": 0}
    djay_ratings = {"a": 3, "b": 4}
    pairs = [("1", "a"), ("2", "b")]

    analysis, ratings = build_analysis_diff(
        rb_analysis, djay_analysis, rb_ratings, djay_ratings, pairs
    )
    assert len(analysis) > 0
    assert len(ratings) == 2

    # Cue comparison (manual).
    rb_cues_a = [NormalisedCue(position_msec=1000, kind="memory")]
    djay_cues_a = [
        NormalisedCue(position_msec=1000, kind="memory"),
        NormalisedCue(position_msec=5000, kind="hot", index=0),
    ]
    _rb_only, djay_only, _conflicts = compare_cue_lists(rb_cues_a, djay_cues_a)
    assert djay_only == [5000]

    res = resolve_cue_array(rb_cues_a, djay_cues_a)
    assert len(res.rb_additions) == 1  # the hot cue missing from RB

    # Emit diffs to tmp and run CLIs in dry-run mode.
    ratings_csv = tmp_path / "ratings-diff.csv"
    analysis_csv = tmp_path / "analysis-diff.csv"
    write_diff_csv(ratings, ratings_csv)
    write_diff_csv(analysis, analysis_csv)

    assert ratings_main(["--diff-csv", str(ratings_csv)]) == 0
    assert analysis_main(["--diff-csv", str(analysis_csv)]) == 0

    # Empty cue-diff via dry-run.
    cue_diff = tmp_path / "cue-diff.csv"
    cue_diff.write_text(
        "rb_content_id,djay_uuid,rb_cue_count,djay_cue_count,"
        "rb_only_positions,djay_only_positions,conflicting_positions,union_count\n"
        "1,a,1,2,,5000,,2\n",
        encoding="utf-8",
    )
    assert cues_main(["--diff-csv", str(cue_diff)]) == 0

    # The smoke is green: the pipeline completes without live writes or
    # exceptions. Live-DB validation is a user-driven runbook (see
    # ``docs/phase-04-cautious-runbook.md`` and ``phase-04-probe-o2-runbook.md``).
