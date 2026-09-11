"""Lane registry: beatgrid is delegated; remaining lanes are not scored as misses."""

from __future__ import annotations

import pytest

from apps.parity.lanes import BEATMAP_OWNER, LANE_IDS, SCORED_THIS_ROUND
from apps.parity.score import render_report, score_payload
from tests.parity.payloads import round0_fixture

pytestmark = pytest.mark.requirement("PARITY-01")


def test_every_enumerated_lane_appears_in_the_report() -> None:
    """PARITY-01 names eleven lanes from what rekordbox holds. A round that
    silently drops one has not scored 'every' lane; it has scored a subset
    and called it the list.
    """
    report = score_payload(round0_fixture())
    assert [figure.lane for figure in report.figures] == list(LANE_IDS)
    assert set(LANE_IDS) == {
        "bpm",
        "key",
        "beatgrid",
        "downbeat",
        "waveform_preview",
        "waveform_detail",
        "waveform_triband",
        "phrase",
        "cues_db",
        "cues_anlz",
        "vocal",
    }


def test_beatgrid_and_downbeat_delegate_to_beatmap_and_do_not_invent_a_metric() -> None:
    """BEATMAP-01 owns the beat-grid lane. PARITY-01 must not restate a
    second, divergent beat metric. Downbeat lives on the same PQTZ n==1
    beats, so it delegates with the grid rather than growing a twin scorer.
    """
    report = score_payload(round0_fixture())
    text = render_report(report)
    for lane in ("beatgrid", "downbeat"):
        figure = report.figure(lane)
        assert figure.status == "delegated"
        assert figure.owner == BEATMAP_OWNER
        assert figure.exact_n is None
        assert figure.agree_ids == ()
        assert figure.disagree_ids == ()
    assert "f-measure" not in text.lower()
    assert "f_measure" not in text.lower()
    assert "median offset" not in text.lower()
    assert BEATMAP_OWNER in text


def test_unscored_lanes_are_not_scored_as_agreement_or_as_a_miss() -> None:
    """Round 1 scores BPM, Key, phrase, cues, and Vocal. Waveform lanes stay
    `not_scored_this_round` with a named reason, never 0-of-N agreement.
    """
    report = score_payload(round0_fixture())
    assert SCORED_THIS_ROUND == (
        "bpm",
        "key",
        "phrase",
        "cues_db",
        "cues_anlz",
        "vocal",
    )
    for figure in report.figures:
        if figure.lane in SCORED_THIS_ROUND:
            assert figure.status == "scored"
            continue
        if figure.lane in {"beatgrid", "downbeat"}:
            assert figure.status == "delegated"
            continue
        assert figure.status == "not_scored_this_round"
        assert figure.agree_ids == ()
        assert figure.disagree_ids == ()
        assert figure.reason
        assert figure.at_parity is False


def test_report_never_claims_any_lane_is_at_parity() -> None:
    """Thresholds are the maintainer-gated. Round 0 may quote counts; it may not
    describe a lane as at parity.
    """
    text = render_report(score_payload(round0_fixture())).lower()
    assert "at parity" not in text
    assert "called at parity" not in text
