"""Map ``EngineCmd`` shapes to v2 control stamps (mirrors ``control_capture.rs``)."""

from __future__ import annotations

from typing import Any, Literal

from . import control_ids as ids
from .control_events import ControlKind, ControlSource, RawControlStamp

CmdName = Literal[
    "play",
    "cue",
    "seek",
    "tempo",
    "tempo_fader",
    "pitch_range",
    "master_tempo",
    "key_nudge",
]


def stamp_from_cmd(
    cmd: CmdName,
    *,
    deck: int,
    sample: int,
    sample_rate: int,
    source: ControlSource = "controller",
    playing: bool | None = None,
    position_ms: float | None = None,
    ratio: float | None = None,
    value: float | None = None,
    range_pct: float | None = None,
    enabled: bool | None = None,
    semitones: int | None = None,
) -> RawControlStamp | None:
    deck = int(deck)
    if deck < 1 or deck > 4:
        return None
    templates: dict[CmdName, tuple[str, ControlKind]] = {
        "play": ("deck.{deck}.play", "set"),
        "cue": ("deck.{deck}.cue", "trigger"),
        "seek": ("deck.{deck}.seek_ms", "set"),
        "tempo": ("deck.{deck}.tempo", "set"),
        "tempo_fader": ("deck.{deck}.tempo_fader", "set"),
        "pitch_range": ("deck.{deck}.pitch_range", "set"),
        "master_tempo": ("deck.{deck}.master_tempo", "set"),
        "key_nudge": ("deck.{deck}.key_nudge", "trigger"),
    }
    template, kind = templates.get(cmd, (None, "set"))  # type: ignore[misc]
    if template is None:
        return None
    control = ids.control_id(template, deck=deck)
    if cmd == "play":
        val: Any = bool(playing)
    elif cmd == "cue":
        val = True
    elif cmd == "seek":
        val = float(position_ms if position_ms is not None else 0.0)
    elif cmd == "tempo":
        val = float(ratio if ratio is not None else 1.0)
    elif cmd == "tempo_fader":
        val = float(value if value is not None else 0.5)
    elif cmd == "pitch_range":
        val = float(range_pct if range_pct is not None else 8.0)
    elif cmd == "master_tempo":
        val = bool(enabled)
    elif cmd == "key_nudge":
        val = float(semitones if semitones is not None else 0)
    else:
        return None
    return RawControlStamp(
        sample=sample,
        sample_rate=sample_rate,
        control=control,
        value=val,
        kind=kind,
        source=source,
        deck=deck,
    )
