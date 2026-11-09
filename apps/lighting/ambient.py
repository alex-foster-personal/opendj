"""LIGHT-10588: ambient mode, an idle look loop when every deck has stopped (issue #9896, spec item LIGHT-55).

Mimics rekordbox Ambient: once every deck has been stopped for ``idle_delay_s`` a slow look
loop runs at its own tempo, dim and without strobe; when a deck plays the show fades back
over one beat. Pure state machine; the look list is data in ``ambient_looks.json``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from apps.lighting.beat_clock import BEATS_PER_BAR, DeckSample
from apps.lighting.show_map import load_look_ids

DEFAULT_AMBIENT_PATH: Final = Path(__file__).with_name("ambient_looks.json")
MAX_INTENSITY: Final = 0.6
MIN_LOOKS: Final = 2
NS_PER_S: Final = 1_000_000_000

_TOP_KEYS: Final = frozenset(
    {"version", "note", "looks_file", "idle_delay_s", "bpm", "bars_per_step", "intensity", "looks"}
)


class AmbientError(ValueError):
    """Raised when the ambient look file fails validation."""

    def __init__(self, source: str, errors: Sequence[str]) -> None:
        self.errors = tuple(errors)
        super().__init__(f"invalid ambient looks {source}:\n" + "\n".join(f"  {e}" for e in self.errors))


class LightMode(StrEnum):
    SHOW = "show"
    AMBIENT = "ambient"
    DARK = "dark"


@dataclass(frozen=True)
class AmbientConfig:
    idle_delay_s: float
    bpm: float
    bars_per_step: int
    intensity: float
    looks: tuple[str, ...]

    @property
    def bar_ns(self) -> int:
        return round(BEATS_PER_BAR * 60 / self.bpm * NS_PER_S)

    @property
    def idle_delay_ns(self) -> int:
        return round(self.idle_delay_s * NS_PER_S)


@dataclass(frozen=True)
class AmbientFrame:
    mode: LightMode
    look_id: str | None
    intensity: float
    strobe: float
    show_mix: float


_SHOW: Final = AmbientFrame(LightMode.SHOW, None, 0.0, 0.0, 1.0)
_DARK: Final = AmbientFrame(LightMode.DARK, None, 0.0, 0.0, 0.0)


# ----------------------------------------------------------------------------- loading


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def parse_ambient_config(data: object, source: str, look_ids: frozenset[str]) -> AmbientConfig:
    if not isinstance(data, dict):
        raise AmbientError(source, ["$: must be an object"])
    errors = [f"$.{key}: unknown key" for key in sorted(set(data) - _TOP_KEYS)]
    errors += [f"$.{key}: missing" for key in sorted(_TOP_KEYS - {"note"} - set(data))]
    if "version" in data and data["version"] != 1:
        errors.append(f"$.version: must be 1, got {data['version']!r}")
    for key in ("idle_delay_s", "bpm"):
        if key in data and not (_is_number(data[key]) and data[key] > 0):
            errors.append(f"$.{key}: must be a number > 0, got {data[key]!r}")
    steps = data.get("bars_per_step")
    if "bars_per_step" in data and not (isinstance(steps, int) and not isinstance(steps, bool) and steps >= 1):
        errors.append(f"$.bars_per_step: must be an integer >= 1, got {steps!r}")
    level = data.get("intensity")
    if "intensity" in data and not (_is_number(level) and 0 < level <= MAX_INTENSITY):
        errors.append(f"$.intensity: must be > 0 and <= {MAX_INTENSITY}, got {level!r}")
    looks = data.get("looks")
    if "looks" in data:
        if not isinstance(looks, list) or not all(isinstance(look, str) for look in looks):
            errors.append("$.looks: must be a list of look id strings")
        elif len(looks) < MIN_LOOKS or len(set(looks)) != len(looks):
            errors.append(f"$.looks: needs at least {MIN_LOOKS} unique look ids, got {looks!r}")
        else:
            errors += [f"$.looks: unknown look id {look!r}" for look in looks if look not in look_ids]
    if errors:
        raise AmbientError(source, errors)
    return AmbientConfig(
        idle_delay_s=float(data["idle_delay_s"]),
        bpm=float(data["bpm"]),
        bars_per_step=int(data["bars_per_step"]),
        intensity=float(data["intensity"]),
        looks=tuple(looks),
    )


def load_ambient_config(path: Path = DEFAULT_AMBIENT_PATH) -> AmbientConfig:
    """Read and validate the ambient look file and the looks catalog it names."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AmbientError(str(path), [f"$: file not found: {exc}"]) from exc
    except json.JSONDecodeError as exc:
        raise AmbientError(str(path), [f"$: not valid JSON: {exc}"]) from exc
    looks_file = data.get("looks_file") if isinstance(data, dict) else None
    if not isinstance(looks_file, str) or not looks_file.strip():
        raise AmbientError(str(path), ["$.looks_file: must be a non-empty string"])
    return parse_ambient_config(data, str(path), load_look_ids(path.parent / looks_file))


# ----------------------------------------------------------------------------- engine


def _beat_ns(bpm: float) -> int:
    return round(60 / bpm * NS_PER_S)


class AmbientEngine:
    """Per-tick decision between the live show, the ambient loop and a dark rig."""

    def __init__(self, config: AmbientConfig, enabled: bool) -> None:
        self._config = config
        self._enabled = enabled
        self._now_ns: int | None = None
        self._last_playing_ns: int | None = None
        self._ambient_start_ns: int | None = None
        self._fade_start_ns: int | None = None
        self._fade_from: AmbientFrame | None = None

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled

    def step(self, samples: Sequence[DeckSample], now_ns: int, show_bpm: float | None) -> AmbientFrame:
        if self._now_ns is not None and now_ns < self._now_ns:
            raise ValueError(f"now_ns went backwards: {now_ns} < {self._now_ns}")
        self._now_ns = now_ns
        if any(sample.playing for sample in samples):
            return self._step_playing(now_ns, show_bpm)
        return self._step_stopped(now_ns)

    def _step_playing(self, now_ns: int, show_bpm: float | None) -> AmbientFrame:
        if show_bpm is None or not show_bpm > 0:
            raise ValueError(f"show_bpm must be a positive number while a deck plays, got {show_bpm!r}")
        self._last_playing_ns = now_ns
        if self._ambient_start_ns is not None:
            self._fade_from = self._ambient_frame(now_ns)
            self._fade_start_ns = now_ns
            self._ambient_start_ns = None
        if self._fade_start_ns is None or self._fade_from is None:
            return _SHOW
        progress = (now_ns - self._fade_start_ns) / _beat_ns(show_bpm)
        if progress >= 1.0:
            self._fade_start_ns = None
            self._fade_from = None
            return _SHOW
        return AmbientFrame(
            LightMode.SHOW, self._fade_from.look_id, self._fade_from.intensity * (1 - progress), 0.0, progress
        )

    def _step_stopped(self, now_ns: int) -> AmbientFrame:
        self._fade_start_ns = None
        self._fade_from = None
        if self._last_playing_ns is None:
            self._last_playing_ns = now_ns
        due_ns = self._last_playing_ns + self._config.idle_delay_ns
        if now_ns < due_ns:
            return _SHOW
        if not self._enabled:
            self._ambient_start_ns = None
            return _DARK
        if self._ambient_start_ns is None:
            self._ambient_start_ns = due_ns
        return self._ambient_frame(now_ns)

    def _ambient_frame(self, now_ns: int) -> AmbientFrame:
        assert self._ambient_start_ns is not None
        cfg = self._config
        bar = (now_ns - self._ambient_start_ns) // cfg.bar_ns
        look = cfg.looks[(bar // cfg.bars_per_step) % len(cfg.looks)]
        return AmbientFrame(LightMode.AMBIENT, look, cfg.intensity, 0.0, 0.0)
