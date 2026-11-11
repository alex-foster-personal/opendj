"""History recency term for suggest-next (issue #10397).

[if] flow history weight penalizes recent plays [then] rested ranks higher, [else stop].
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from apps.dj_copilot.history_term import (
    HISTORY_WEIGHT_BY_VARIANT,
    REST_DAYS,
    history_term,
)
from apps.dj_copilot.rank_stage1 import rank_stage1
from apps.dj_copilot.session_context import PlayedTrack
from apps.shared.harmonic import TrackFeature, track_compatibility
from apps.webui.server.backend import Track
from apps.webui.server.track_features import track_to_feature
from tests.dj_copilot.conftest import FIXED_NOW

pytestmark = pytest.mark.requirement("SETPREP-12113")


def _feat(**kw: object) -> TrackFeature:
    defaults: dict[str, object] = {
        "stable_id": "t1",
        "artist": "A",
        "bpm": 120.0,
        "key_camelot": "8A",
        "energy": 5,
    }
    defaults.update(kw)
    return TrackFeature(**defaults)  # type: ignore[arg-type]


def test_never_played_scores_one() -> None:
    """[if] never_played is set [then ⛔️] term is 1.0, [else stop]."""
    assert history_term(_feat(never_played=True), FIXED_NOW) == 1.0


def test_unknown_last_played_unscored() -> None:
    """[if] last_played_at is missing [then ⛔️] term is None, [else stop]."""
    assert history_term(_feat(), FIXED_NOW) is None


def test_rested_track_scores_one() -> None:
    """[if] played 14+ days ago [then ⛔️] term is 1.0, [else stop]."""
    for days in (14, 30):
        played = FIXED_NOW - timedelta(days=days)
        assert history_term(_feat(last_played_at=played), FIXED_NOW) == 1.0


def test_mid_rest_linear() -> None:
    """[if] played 7 days ago [then ⛔️] term is 0.5, [else stop]."""
    played = FIXED_NOW - timedelta(days=7)
    assert history_term(_feat(last_played_at=played), FIXED_NOW) == pytest.approx(0.5)


def test_played_today_scores_zero() -> None:
    """[if] played at now [then ⛔️] term is 0.0, [else stop]."""
    assert history_term(_feat(last_played_at=FIXED_NOW), FIXED_NOW) == 0.0


def test_future_last_played_clamps_zero() -> None:
    """[if] last_played is in the future [then ⛔️] term is 0.0, [else stop]."""
    future = FIXED_NOW + timedelta(days=1)
    assert history_term(_feat(last_played_at=future), FIXED_NOW) == 0.0


def test_naive_now_raises() -> None:
    """[if] now is naive [then ⛔️] ValueError, [else stop]."""
    with pytest.raises(ValueError):
        history_term(_feat(), datetime(2025, 1, 1, 12, 0, 0))  # noqa: DTZ001 intentional naive now


def test_variant_weights() -> None:
    """[if] HISTORY_WEIGHT_BY_VARIANT is read [then ⛔️] flow is 0.10, [else stop]."""
    assert dict(HISTORY_WEIGHT_BY_VARIANT) == {"harmonic": 0.0, "energy_led": 0.0, "flow": 0.10}
    assert REST_DAYS == 14.0


def test_rank_stage1_weight_zero_unchanged() -> None:
    """[if] history weight is 0 [then ⛔️] score equals track_compatibility, [else stop]."""
    current = PlayedTrack(
        stable_id="cur",
        artist="Cur",
        bpm=120.0,
        key_camelot="8A",
        energy=5,
        played_at=FIXED_NOW,
    )
    played = FIXED_NOW - timedelta(days=3)
    candidates = [
        TrackFeature("a", "A", 121.0, "8A", 5, last_played_at=played),
        TrackFeature("b", "B", 121.0, "8A", 5),
    ]
    ranked = rank_stage1(current_track=current, candidates=candidates, history_weight=0.0)
    for sc in ranked:
        cand = next(c for c in candidates if c.stable_id == sc.stable_id)
        expected = round(
            track_compatibility("8A", 120.0, 5, cand.key_camelot, cand.bpm or 0.0, cand.energy or 0),
            6,
        )
        assert sc.score == expected
        assert set(sc.rationale) == {"camelot", "bpm", "energy"}


def test_rank_stage1_flow_prefers_rested() -> None:
    """[if] flow weight and equal compat [then ⛔️] rested ranks first, [else stop]."""
    current = PlayedTrack(
        stable_id="cur",
        artist="Cur",
        bpm=120.0,
        key_camelot="8A",
        energy=5,
        played_at=FIXED_NOW,
    )
    recent = FIXED_NOW - timedelta(days=2)
    rested = FIXED_NOW - timedelta(days=20)
    candidates = [
        TrackFeature("recent", "A", 121.0, "8A", 5, last_played_at=recent),
        TrackFeature("rested", "B", 121.0, "8A", 5, last_played_at=rested),
        TrackFeature("unknown", "C", 121.0, "8A", 5),
    ]
    w = HISTORY_WEIGHT_BY_VARIANT["flow"]
    ranked = rank_stage1(
        current_track=current,
        candidates=candidates,
        history_weight=w,
        now=FIXED_NOW,
    )
    assert ranked[0].stable_id == "rested"
    recent_sc = next(sc for sc in ranked if sc.stable_id == "recent")
    assert recent_sc.rationale["days_since_played"] == 2.0
    assert recent_sc.rationale["history"] == pytest.approx(2.0 / 14.0)
    unknown_sc = next(sc for sc in ranked if sc.stable_id == "unknown")
    assert "history" not in unknown_sc.rationale


def test_rank_stage1_weight_without_now_raises() -> None:
    """[if] history weight positive without now [then ⛔️] ValueError, [else stop]."""
    current = PlayedTrack(
        stable_id="cur",
        artist="Cur",
        bpm=120.0,
        key_camelot="8A",
        energy=5,
        played_at=FIXED_NOW,
    )
    with pytest.raises(ValueError):
        rank_stage1(
            current_track=current,
            candidates=[TrackFeature("a", "A", 121.0, "8A", 5)],
            history_weight=0.1,
        )


def test_track_to_feature_last_played_and_title() -> None:
    """[if] Track has last_played_at strings [then ⛔️] feature parses UTC, [else stop]."""
    tf = track_to_feature(Track(stable_id="s1", title="My Song", last_played_at="2025-01-01"))
    assert tf.title == "My Song"
    assert tf.last_played_at == datetime(2025, 1, 1, tzinfo=UTC)
    tf2 = track_to_feature(
        Track(stable_id="s2", last_played_at="2025-01-01T10:00:00+02:00")
    )
    assert tf2.last_played_at == datetime(2025, 1, 1, 8, 0, 0, tzinfo=UTC)
    tf3 = track_to_feature(Track(stable_id="s3", last_played_at="garbage"))
    assert tf3.last_played_at is None
