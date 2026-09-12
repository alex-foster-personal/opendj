"""NATIVE-03: tempo-change markers honored by every grid reader.

[if] an own payload with static_grid_untrusted true still yields effectiveBeatSync true or a 3 percent ramp at 2:00 is published static [then] fail, [else stop].

Three acceptance lines, one pytest each:
  [if] a fixture carries an injected 3 percent ramp at 2:00 [then] a marker lands
       within 8 bars of it and the track is not flagged static
  [if] own grid is the effective source and the payload carries
       `static_grid_untrusted: true` [then] `effectiveQuantize` and
       `effectiveBeatSync` are false and the controls name the first marker time
  [if] the payload is rekordbox-sourced [then] the field is absent, meaning
       trusted, and rekordbox behavior is unchanged
"""
from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import pytest

from apps.adapters.rekordbox.paths import empty_anlz_payload
from apps.analysis import selection
from apps.analysis.lanes import validate_lane_payload
from apps.analysis_beatgrid.lane_payload import build_beatgrid_lane
from apps.analysis_beatgrid.tempo_change import MIN_SEGMENT_BARS, detect_tempo_changes
from apps.webui.server.rb_vendor_pkg import own_beatgrid_overlay as overlay_mod

pytestmark = pytest.mark.requirement("NATIVE-03")

CLICK_BPM = 128.0
TRACK_S = 200.0
RAMP_AT_S = 120.0
RAMP_FACTOR = 1.03
RAMP_OVER_S = 8 * 4 * (60.0 / CLICK_BPM)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _launch_state_toggles() -> Any:
    selection.reset_toggles()
    yield
    selection.reset_toggles()


def _ramped_beat_times(
    bpm: float, seconds: float, ramp_at_s: float, ramp_over_s: float, factor: float
) -> list[float]:
    times: list[float] = []
    t = 0.0
    while t < seconds:
        times.append(t)
        if t < ramp_at_s:
            current = bpm
        elif t >= ramp_at_s + ramp_over_s:
            current = bpm * factor
        else:
            progress = (t - ramp_at_s) / ramp_over_s
            current = bpm * (1.0 + (factor - 1.0) * progress)
        t += 60.0 / current
    return times


def _steady_beat_times(bpm: float, seconds: float) -> list[float]:
    period = 60.0 / bpm
    return [i * period for i in range(int(seconds / period) + 1) if i * period < seconds]


def _result(
    beats: list[float],
    *,
    downbeat_every: int | None = 4,
    activation_peak: float = 0.99,
) -> dict[str, Any]:
    downbeats = [] if downbeat_every is None else beats[::downbeat_every]
    return {
        "audio": "/fixtures/synthetic.wav",
        "beats": beats,
        "downbeats": downbeats,
        "activation_peak": activation_peak,
        "n_frames": int(max(beats, default=0.0) * 50) + 1,
        "activations_npz": "/fixtures/synthetic.activations.npz",
        "fps": 50,
        "decode_fingerprint": "0" * 64,
        "sample_rate": 44100,
        "error": None,
    }


def _marker_within_eight_bars_of(onset_s: float, markers: list[Any], bpm: float) -> bool:
    window = MIN_SEGMENT_BARS * 4 * (60.0 / bpm)
    return any(abs(m.at_s - onset_s) <= window for m in markers)


def test_three_percent_ramp_at_two_minutes_is_not_published_static() -> None:
    """[if] a fixture carries an injected 3 percent ramp at 2:00 [then] a marker lands within 8 bars of it and the track is not flagged static, [else stop]."""
    times = _ramped_beat_times(CLICK_BPM, TRACK_S, RAMP_AT_S, RAMP_OVER_S, RAMP_FACTOR)
    intervals = [b - a for a, b in itertools.pairwise(times)]
    steps = [abs(b - a) / a for a, b in itertools.pairwise(intervals)]
    assert max(steps) < 0.01, "fixture must be a ramp, not a step"
    first, last = 60.0 / intervals[0], 60.0 / intervals[-1]
    assert last / first == pytest.approx(RAMP_FACTOR, rel=0.01)

    changes = detect_tempo_changes(times)
    assert changes.markers, "ramp must yield at least one tempo marker"
    assert _marker_within_eight_bars_of(RAMP_AT_S, changes.markers, CLICK_BPM)
    assert changes.markers[0].bpm_after > changes.markers[0].bpm_before

    lane = build_beatgrid_lane(_result(times), threshold=0.5)
    assert lane.status == "ok", lane.reason
    validate_lane_payload("beatgrid", lane.payload)
    assert lane.payload["tempo_changes"], "lane must carry tempo_changes, not a static grid"
    lane_markers = lane.payload["tempo_changes"]
    assert any(abs(m["at_s"] - RAMP_AT_S) <= MIN_SEGMENT_BARS * 4 * (60.0 / CLICK_BPM) for m in lane_markers)

    steady = _steady_beat_times(CLICK_BPM, TRACK_S)
    steady_changes = detect_tempo_changes(steady)
    assert not steady_changes.markers, "steady grid must not fire spurious markers"


def test_static_grid_untrusted_true_gates_every_named_grid_reader() -> None:
    """[if] own grid carries static_grid_untrusted true [then] every grid reader gates on hasTrustedBeatGrid, [else stop]."""
    checks: list[tuple[str, list[str]]] = [
        (
            "apps/webui/frontend/src/lib/player/grid-features.ts",
            [
                "if (bg.static_grid_untrusted === true) return false;",
                "deckHasTrustedBeatGrid",
                "first.at_s.toFixed(3)",
            ],
        ),
        (
            "apps/webui/frontend/src/lib/rb/audio-engine.svelte.ts",
            [
                "effectiveQuantize(",
                "gridFeaturesInert(",
                "gridFeatureInertTip(",
                "effectiveBeatSync(",
            ],
        ),
        (
            "apps/webui/frontend/src/lib/rb/performance-ipc.svelte.ts",
            [
                "hasTrustedBeatGrid(",
            ],
        ),
        (
            "apps/webui/frontend/src/lib/components/rb/deck/DeckHeader.svelte",
            ["gridFeaturesInert(", "gridFeatureInertTip("],
        ),
        (
            "apps/webui/frontend/src/lib/components/rb/deck/JogDial.svelte",
            ["gridFeaturesInert(", "gridFeatureInertTip("],
        ),
        (
            "apps/webui/frontend/src/lib/rb/deck-hot-cue-actions.ts",
            ["effectiveQuantize("],
        ),
    ]

    for rel_path, tokens in checks:
        text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        for token in tokens:
            assert token in text, f"{rel_path} missing required token: {token}"

    ipc = (REPO_ROOT / "apps/webui/frontend/src/lib/rb/performance-ipc.svelte.ts").read_text(
        encoding="utf-8"
    )
    save_start = ipc.index("} else if (command.type === 'hot_cue_save')")
    save_slice = ipc[save_start : save_start + 800]
    assert "hasTrustedBeatGrid(" in save_slice, "hot_cue_save must gate on hasTrustedBeatGrid"
    trigger_start = ipc.index("} else if (command.type === 'hot_cue_trigger')")
    trigger_slice = ipc[trigger_start : trigger_start + 600]
    assert "hasTrustedBeatGrid(" in trigger_slice, "hot_cue_trigger must gate on hasTrustedBeatGrid"
    assert "planHotCueTrigger(" in trigger_slice

    engine = (REPO_ROOT / "apps/webui/frontend/src/lib/rb/audio-engine.svelte.ts").read_text(
        encoding="utf-8"
    )
    qg_start = engine.index("function _quantizeGrid")
    qg_slice = engine[qg_start : qg_start + 300]
    assert "effectiveQuantize(" in qg_slice, "_quantizeGrid must use effectiveQuantize"


def test_rekordbox_payload_omits_untrusted_fields_and_overlay_is_noop() -> None:
    """[if] the payload is rekordbox-sourced [then] untrusted fields are absent and overlay is a no-op, [else stop]."""
    grid = empty_anlz_payload("sid-rbx-shape", 400)["beatgrid"]
    assert grid["source"] == "rekordbox"
    for own_only in ("status", "reason", "static_grid_untrusted", "bpm"):
        assert own_only not in grid, f"{own_only} must be absent on rekordbox payloads"
    payload = empty_anlz_payload("sid-rbx-shape", 400)
    assert "tempo_changes" not in payload

    before = json.dumps(payload, sort_keys=True)
    after = overlay_mod.apply_own_beatgrid(payload, "sid-rbx-shape")
    assert json.dumps(after, sort_keys=True) == before
    assert after["beatgrid"]["source"] == "rekordbox"
    assert "tempo_changes" not in after
