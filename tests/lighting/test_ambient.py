"""LIGHT-10588: ambient idle look loop when every deck has stopped (issue #9896).

[if] both decks stop for 4 s in a replay [then] ambient starts and frames carry no strobe, [else stop].
[if] a deck starts playing [then] the show is back within one beat, [else stop].
[if] ambient is disabled by override [then] stopped decks give a dark rig, not ambient, [else stop].
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from apps.lighting.ambient import (
    DEFAULT_AMBIENT_PATH,
    MAX_INTENSITY,
    AmbientEngine,
    AmbientError,
    AmbientFrame,
    LightMode,
    load_ambient_config,
)
from apps.lighting.beat_clock import NS_PER_MS, DeckSample

pytestmark = [pytest.mark.requirement("LIGHT-10588")]

TICK_MS = 33
SHOW_BPM = 128.0
BEAT_MS = 60_000 / SHOW_BPM


def _replay(engine: AmbientEngine, playing: Callable[[str, int], bool], end_ms: int) -> list[tuple[int, AmbientFrame]]:
    frames = []
    for t_ms in range(0, end_ms, TICK_MS):
        now_ns = t_ms * NS_PER_MS
        samples = [DeckSample(deck, float(t_ms), 1.0, playing(deck, t_ms), now_ns) for deck in ("A", "B")]
        bpm = SHOW_BPM if any(s.playing for s in samples) else None
        frames.append((t_ms, engine.step(samples, now_ns, show_bpm=bpm)))
    return frames


def _stop_at_2s(deck: str, t_ms: int) -> bool:
    return t_ms < 2000


def test_both_decks_stopped_for_four_seconds_starts_ambient_without_strobe() -> None:
    config = load_ambient_config()
    frames = _replay(AmbientEngine(config, enabled=True), _stop_at_2s, 30_000)
    assert all(f.mode is LightMode.SHOW for t, f in frames if t < 6000)
    first = next(t for t, f in frames if f.mode is LightMode.AMBIENT)
    assert 6000 <= first < 6000 + TICK_MS
    ambient = [f for t, f in frames if t >= first]
    assert all(f.mode is LightMode.AMBIENT for f in ambient)
    assert all(f.strobe == 0.0 and 0 < f.intensity <= MAX_INTENSITY for f in ambient)
    assert all(f.look_id in config.looks for f in ambient)


def test_ambient_steps_look_every_four_bars_at_100_bpm() -> None:
    config = load_ambient_config()
    frames = _replay(AmbientEngine(config, enabled=True), _stop_at_2s, 30_000)
    changes = []
    previous = None
    for t, f in frames:
        if f.mode is LightMode.AMBIENT and f.look_id != previous:
            changes.append((t, f.look_id))
            previous = f.look_id
    start = 6000
    assert [look for _, look in changes] == list(config.looks[:3])
    for (t, _), due in zip(changes, (start, start + 9600, start + 19_200), strict=True):
        assert due <= t < due + TICK_MS


def test_deck_playing_brings_the_show_back_within_one_beat() -> None:
    frames = _replay(
        AmbientEngine(load_ambient_config(), enabled=True),
        lambda deck, t: deck == "A" and t >= 10_000,
        14_000,
    )
    assert any(f.mode is LightMode.AMBIENT for t, f in frames if t < 10_000)
    after = [(t, f) for t, f in frames if t >= 10_000]
    assert all(f.mode is LightMode.SHOW and f.strobe == 0.0 for _, f in after)
    mixes = [f.show_mix for _, f in after]
    assert mixes == sorted(mixes)
    back = [f for t, f in after if t >= 10_000 + BEAT_MS + TICK_MS]
    assert back and all(f.show_mix == 1.0 and f.intensity == 0.0 for f in back)


def test_disabled_ambient_gives_a_dark_rig() -> None:
    engine = AmbientEngine(load_ambient_config(), enabled=False)
    frames = _replay(engine, lambda deck, t: False, 10_000)
    assert not any(f.mode is LightMode.AMBIENT for _, f in frames)
    dark = [f for t, f in frames if t >= 4000 + TICK_MS]
    assert dark and all(f.mode is LightMode.DARK and f.intensity == 0.0 and f.look_id is None for f in dark)
    engine.set_enabled(True)
    frame = engine.step([], 10_000 * NS_PER_MS, show_bpm=None)
    assert frame.mode is LightMode.AMBIENT


def test_deck_playing_before_the_delay_never_starts_ambient() -> None:
    frames = _replay(
        AmbientEngine(load_ambient_config(), enabled=True),
        lambda deck, t: t >= 3000,
        8000,
    )
    assert not any(f.mode is LightMode.AMBIENT for _, f in frames)


def test_playing_without_show_bpm_fails_fast() -> None:
    engine = AmbientEngine(load_ambient_config(), enabled=True)
    with pytest.raises(ValueError, match="show_bpm"):
        engine.step([DeckSample("A", 0.0, 1.0, True, 0)], 0, show_bpm=None)


def test_shipped_config_defaults() -> None:
    config = load_ambient_config()
    assert config.idle_delay_s == 4.0
    assert config.bpm == 100.0
    assert config.bars_per_step == 4
    assert config.intensity <= MAX_INTENSITY


def _write_variant(tmp_path: Path, mutate: Callable[[dict], None]) -> Path:
    shutil.copy(DEFAULT_AMBIENT_PATH.with_name("looks_default.json"), tmp_path / "looks_default.json")
    data = json.loads(DEFAULT_AMBIENT_PATH.read_text(encoding="utf-8"))
    mutate(data)
    path = tmp_path / "ambient_looks.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda d: d.update(intensity=0.7), "0.6"),
        (lambda d: d.update(looks=["wash_dim", "laser_storm"]), "laser_storm"),
        (lambda d: d.update(looks=["wash_dim"]), "at least 2"),
        (lambda d: d.update(strobe=1.0), "strobe: unknown key"),
        (lambda d: d.pop("bpm"), "bpm: missing"),
    ],
)
def test_invalid_ambient_file_fails_naming_the_problem(
    tmp_path: Path, mutate: Callable[[dict], None], match: str
) -> None:
    with pytest.raises(AmbientError, match=match):
        load_ambient_config(_write_variant(tmp_path, mutate))
