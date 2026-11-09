"""LIGHT-10578: WCAG 2.3.1 photosensitive flash limiter caps output at 3 flashes per second.

[if] a strobe exceeds 3 flashes per second in any 1 s window [then] fail, [else stop].
[if] hardware strobe_hz or rig-wide sum is not clamped to budget [then] fail, [else stop].
[if] safe 2 Hz strobes change, red rule misses, or cap setting above 3 loads [then] fail, [else stop].
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from apps.lighting.safety import (
    FixtureLight,
    FlashLimiter,
    SafetySettingsError,
    load_safety_settings,
    relative_luminance,
)

pytestmark = [pytest.mark.requirement("LIGHT-10578")]

FPS = 40
BLACK = FixtureLight(rgb=(0, 0, 0), intensity=0.0)


def _square(
    hz: float,
    seconds: float,
    on: FixtureLight,
    off: FixtureLight = BLACK,
    phase_frames: int = 0,
) -> Iterator[tuple[float, FixtureLight]]:
    toggle_every = max(1, round(FPS / (2 * hz)))
    total = int(seconds * FPS)
    for k in range(total):
        toggles = (k + phase_frames) // toggle_every
        yield k / FPS, on if toggles % 2 == 0 else off


def _max_change_times_in_window(change_times: list[float]) -> int:
    if not change_times:
        return 0
    best = 0
    for anchor in change_times:
        window_start = anchor - 1.0
        count = sum(1 for ct in change_times if window_start < ct <= anchor)
        best = max(best, count)
    return best


def _max_changes_per_window(times: list[float], values: list[float], threshold: float) -> int:
    prev = 0.0
    change_times: list[float] = []
    for t, v in zip(times, values, strict=True):
        if abs(v - prev) >= threshold:
            change_times.append(t)
        prev = v
    if not change_times:
        return 0
    best = 0
    for anchor in change_times:
        window_start = anchor - 1.0
        count = sum(1 for ct in change_times if window_start < ct <= anchor)
        best = max(best, count)
    return best


def _run_frames(
    limiter: FlashLimiter,
    frames: Iterator[tuple[float, dict[str, FixtureLight]]],
) -> list[tuple[float, dict[str, FixtureLight]]]:
    out: list[tuple[float, dict[str, FixtureLight]]] = []
    for now, frame in frames:
        out.append((now, limiter.limit(now, frame)))
    return out


def _red_value(light: FixtureLight) -> float:
    from apps.lighting.safety import _linear_rgb, _red_value_linear

    return _red_value_linear(_linear_rgb(light))


def test_a_10_hz_full_intensity_strobe_is_held_to_3_flashes_in_every_window() -> None:
    """[if] a 10 Hz full-intensity strobe runs 2 s [then] every 1 s window has at most 3 flashes."""
    limiter = FlashLimiter()
    on = FixtureLight(rgb=(255, 255, 255), intensity=1.0)
    frames = ((t, {"par1": light}) for t, light in _square(10.0, 2.0, on))
    outputs = _run_frames(limiter, frames)
    times = [t for t, _ in outputs]
    lums = [relative_luminance(out["par1"]) for _, out in outputs]
    assert _max_changes_per_window(times, lums, 0.10) <= 6
    assert limiter.status().limit_events > 0


def test_a_10_hz_hardware_strobe_on_a_steady_frame_is_clamped_to_3() -> None:
    """[if] a steady frame commands 10 Hz hardware strobe [then] output strobe_hz is at most 3."""
    limiter = FlashLimiter()
    steady = FixtureLight(rgb=(255, 255, 255), intensity=1.0, strobe_hz=10.0)
    outputs: list[FixtureLight] = []
    for k in range(FPS + 1):
        now = k / FPS
        out = limiter.limit(now, {"par1": steady})
        outputs.append(out["par1"])
    for light in outputs:
        assert light.strobe_hz is None or light.strobe_hz <= 3.0 + 1e-9
    assert outputs[-1].strobe_hz == pytest.approx(3.0)
    assert limiter.status().fixture_flashes["par1"] == pytest.approx(3.0)
    assert limiter.status().limit_events > 0


def test_four_2_hz_hardware_strobes_share_the_rig_budget_of_3() -> None:
    """[if] four fixtures each command 2 Hz hardware strobe [then] rig total stays at most 3 per second."""
    limiter = FlashLimiter()
    steady = FixtureLight(rgb=(128, 128, 128), intensity=1.0, strobe_hz=2.0)
    for k in range(FPS * 2):
        now = k / FPS
        frame = {f"par{i}": steady for i in range(1, 5)}
        out = limiter.limit(now, frame)
        total_hz = sum((light.strobe_hz or 0.0) for light in out.values())
        assert total_hz <= 3.0 + 1e-9
        for light in out.values():
            assert light.strobe_hz is None or light.strobe_hz <= 2.0 + 1e-9
    assert limiter.status().rig_flashes <= 3.0 + 1e-9
    assert limiter.status().limit_events > 0


def test_a_2_hz_strobe_passes_unchanged() -> None:
    """[if] a 2 Hz white strobe runs 2 s [then] every output equals its input."""
    limiter = FlashLimiter()
    on = FixtureLight(rgb=(255, 255, 255), intensity=1.0)
    for t, light in _square(2.0, 2.0, on):
        inp = {"par1": light}
        out = limiter.limit(t, inp)
        assert out["par1"] == inp["par1"]
    assert limiter.status().limit_events == 0


def test_eight_out_of_phase_2_hz_strobes_are_limited_rig_wide() -> None:
    """[if] eight fixtures strobe 2 Hz out of phase [then] rig-wide limiting applies."""
    limiter = FlashLimiter()
    on = FixtureLight(rgb=(255, 255, 255), intensity=1.0)
    toggle_every = max(1, round(FPS / (2 * 2.0)))
    per_fixture: dict[str, list[tuple[float, float]]] = {f"par{i + 1}": [] for i in range(8)}
    for k in range(FPS * 2):
        now = k / FPS
        frame: dict[str, FixtureLight] = {}
        for i in range(8):
            offset = round(i * FPS / 16)
            toggles = (k + offset) // toggle_every
            frame[f"par{i + 1}"] = on if toggles % 2 == 0 else BLACK
        out = limiter.limit(now, frame)
        for fid, light in out.items():
            per_fixture[fid].append((now, relative_luminance(light)))
    merged_changes: list[float] = []
    for samples in per_fixture.values():
        prev = 0.0
        for t, lum in samples:
            if abs(lum - prev) >= 0.10:
                merged_changes.append(t)
            prev = lum
    assert limiter.status().limit_events > 0
    assert _max_change_times_in_window(merged_changes) <= 6


def test_a_saturated_red_at_4_hz_is_limited_at_low_luminance_change() -> None:
    """[if] saturated red flashes at 4 Hz with small luminance delta [then] red rule limits it."""
    red_on = FixtureLight(rgb=(153, 0, 0), intensity=1.0)
    red_off = FixtureLight(rgb=(0, 0, 0), intensity=0.0)
    assert relative_luminance(red_on) < 0.10

    limiter = FlashLimiter()
    times: list[float] = []
    reds: list[float] = []
    for t, light in _square(4.0, 2.0, red_on, red_off):
        out = limiter.limit(t, {"par1": light})
        times.append(t)
        reds.append(_red_value(out["par1"]))
    assert limiter.status().limit_events > 0
    assert _max_changes_per_window(times, reds, 20.0) <= 6

    grey = FixtureLight(rgb=(74, 74, 74), intensity=1.0)
    assert relative_luminance(grey) < 0.10
    limiter2 = FlashLimiter()
    for t, light in _square(4.0, 2.0, grey):
        inp = {"par1": light}
        assert limiter2.limit(t, inp)["par1"] == inp["par1"]
    assert limiter2.status().limit_events == 0


def test_a_rig_mean_reversal_without_individual_changes_is_held() -> None:
    """[if] two fixtures continue prior directions and rig mean reverses (PRRT_kwDOSEvNd861Nv6t)
    [then] rig_flashes stays at most 3 and contributors are held."""
    limiter = FlashLimiter()

    def white(intensity: float) -> FixtureLight:
        return FixtureLight(rgb=(255, 255, 255), intensity=intensity)

    strobe_b = FixtureLight(rgb=(255, 255, 255), intensity=1.0, strobe_hz=3.0)
    frames = [
        (0.0, {"a": white(1.0), "b": white(0.0)}),
        (0.1, {"a": white(0.74), "b": white(0.74)}),
        (1.2, {"a": white(0.74), "b": strobe_b}),
        (1.3, {"a": white(0.0), "b": strobe_b}),
    ]
    outputs = _run_frames(limiter, iter(frames))
    for _t, _out in outputs:
        assert limiter.status().rig_flashes <= 3.0 + 1e-9
    prev_out = outputs[-2][1]
    final = outputs[-1][1]
    prev_mean = (relative_luminance(prev_out["a"]) + relative_luminance(prev_out["b"])) / 2.0
    final_mean = (relative_luminance(final["a"]) + relative_luminance(final["b"])) / 2.0
    assert abs(final_mean - prev_mean) < 0.10 + 1e-9
    assert final["a"].intensity == pytest.approx(0.74)
    assert final["b"].intensity == 1.0
    assert final["b"].strobe_hz is not None
    assert final["b"].strobe_hz <= 3.0 + 1e-9
    assert limiter.status().limit_events > 0


@pytest.mark.requirement("LIGHT-10619")
def test_subthreshold_red_peak_extension_preserves_saturation_for_flash_counting() -> None:
    """[if] saturated peak then unsat extension then 20 Hz red flash [then] limiter counts and limits."""
    limiter = FlashLimiter()
    black = FixtureLight(rgb=(0, 0, 0), intensity=0.0)
    frames: list[tuple[float, FixtureLight]] = [
        (0.0, FixtureLight(rgb=(89, 0, 0), intensity=1.0)),
        (0.025, FixtureLight(rgb=(124, 48, 48), intensity=1.0)),
    ]
    for k in range(80):
        t = 0.05 + k * 0.025
        frames.append((t, FixtureLight(rgb=(105, 0, 0), intensity=1.0) if k % 2 == 0 else black))

    times: list[float] = []
    reds: list[float] = []
    for now, light in frames:
        out = limiter.limit(now, {"par1": light})
        times.append(now)
        reds.append(_red_value(out["par1"]))

    assert limiter.status().limit_events > 0
    assert _max_changes_per_window(times, reds, 20.0) <= 6


def test_a_cap_setting_of_5_fails_naming_the_3_per_second_maximum(tmp_path: Path) -> None:
    """[if] flash_limit_per_second is 5 [then] load fails naming the 3 per second maximum."""
    path = tmp_path / "lighting.json"
    path.write_text(json.dumps({"sacn_cid": "00112233-4455-6677-8899-aabbccddeeff", "flash_limit_per_second": 5}))
    with pytest.raises(SafetySettingsError, match=r"3 flashes per second"):
        load_safety_settings(path)
    missing = tmp_path / "missing.json"
    assert load_safety_settings(missing).flash_limit_per_second == 3.0

    low_path = tmp_path / "low.json"
    low_path.write_text(json.dumps({"flash_limit_per_second": 2}))
    limiter = FlashLimiter(load_safety_settings(low_path))
    on = FixtureLight(rgb=(255, 255, 255), intensity=1.0)
    for t, light in _square(2.5, 2.0, on):
        limiter.limit(t, {"par1": light})
    assert limiter.status().limit_events > 0
