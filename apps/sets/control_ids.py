"""Stable control-event ids shared by engine, page and replay (SETREC-03/05).

Regenerate the page module with ``uv run python -m apps.sets.control_ids --write``.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

ControlKind = Literal["set", "trigger"]
ControlSource = Literal["user", "controller", "agent", "replay"]

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
GENERATED_TS_REL: Final[str] = "apps/webui/frontend/src/lib/sets/control-ids.generated.ts"

# 10 ms at 48 kHz; coalescing window for continuous controls (spec section 4).
COALESCE_QUANTUM_MS: Final[float] = 10.0


@dataclass(frozen=True, slots=True)
class ControlSpec:
    id: str
    kind: ControlKind
    continuous: bool
    deck: int | None = None


# Transport + tempo slice (SETREC-04); mixer/FX/loops/stems land in sibling issues.
TRANSPORT_TEMPO_CONTROLS: Final[tuple[ControlSpec, ...]] = (
    ControlSpec("deck.{deck}.play", "set", False),
    ControlSpec("deck.{deck}.cue", "trigger", False),
    ControlSpec("deck.{deck}.seek_ms", "set", False),
    ControlSpec("deck.{deck}.tempo", "set", True),
    ControlSpec("deck.{deck}.tempo_fader", "set", True),
    ControlSpec("deck.{deck}.pitch_range", "set", False),
    ControlSpec("deck.{deck}.master_tempo", "set", False),
    ControlSpec("deck.{deck}.key_nudge", "trigger", False),
)

KNOWN_CONTROLS: Final[tuple[ControlSpec, ...]] = TRANSPORT_TEMPO_CONTROLS


def control_id(template: str, *, deck: int) -> str:
    return template.replace("{deck}", str(deck))


def is_known_control(control: str) -> bool:
    for spec in KNOWN_CONTROLS:
        if "{deck}" in spec.id:
            prefix = spec.id.split(".")[0] + "."
            suffix = "." + ".".join(spec.id.split(".")[2:])
            if control.startswith(prefix) and control.endswith(suffix):
                mid = control[len(prefix) : -len(suffix)]
                if mid.isdigit() and 1 <= int(mid) <= 4:
                    return True
        elif control == spec.id:
            return True
    return False


def spec_for_control(control: str) -> ControlSpec | None:
    for spec in KNOWN_CONTROLS:
        if "{deck}" not in spec.id:
            if control == spec.id:
                return spec
            continue
        parts = spec.id.split(".")
        if len(parts) < 3:
            continue
        prefix = f"{parts[0]}."
        suffix = "." + ".".join(parts[2:])
        if control.startswith(prefix) and control.endswith(suffix):
            mid = control[len(prefix) : -len(suffix)]
            if mid.isdigit() and 1 <= int(mid) <= 4:
                return spec
    return None


def deck_from_control(control: str) -> int | None:
    if not control.startswith("deck."):
        return None
    rest = control[5:]
    deck_s, _, _ = rest.partition(".")
    if deck_s.isdigit():
        d = int(deck_s)
        if 1 <= d <= 4:
            return d
    return None


#: All mixer control ids the rust engine records (SETREC-05).
ENGINE_MIXER_CONTROL_IDS: Final[frozenset[str]] = frozenset(
    {
        "deck.1.trim",
        "deck.2.trim",
        "deck.3.trim",
        "deck.4.trim",
        "deck.1.auto_gain",
        "deck.2.auto_gain",
        "deck.3.auto_gain",
        "deck.4.auto_gain",
        "deck.1.eq.low",
        "deck.1.eq.mid",
        "deck.1.eq.high",
        "deck.2.eq.low",
        "deck.2.eq.mid",
        "deck.2.eq.high",
        "deck.3.eq.low",
        "deck.3.eq.mid",
        "deck.3.eq.high",
        "deck.4.eq.low",
        "deck.4.eq.mid",
        "deck.4.eq.high",
        "deck.1.filter",
        "deck.2.filter",
        "deck.3.filter",
        "deck.4.filter",
        "deck.1.fader",
        "deck.2.fader",
        "deck.3.fader",
        "deck.4.fader",
        "deck.1.assign",
        "deck.2.assign",
        "deck.3.assign",
        "deck.4.assign",
        "mixer.crossfader",
        "mixer.master_volume",
        "mixer.master_mute",
    }
)

#: Mixer ids the performance page may log when the engine reports `unsupported`.
PAGE_MIXER_CONTROL_IDS: Final[frozenset[str]] = frozenset()

#: Scripted mixer session coverage denominator (spec section 11 family P2).
SCRIPTED_MIXER_CONTROL_IDS: Final[tuple[str, ...]] = tuple(sorted(ENGINE_MIXER_CONTROL_IDS))


def assert_engine_page_mixer_disjoint() -> None:
    """Fail when any mixer id would be logged by both engine and page."""
    overlap = ENGINE_MIXER_CONTROL_IDS & PAGE_MIXER_CONTROL_IDS
    if overlap:
        raise AssertionError(f"mixer control ids claimed by both engine and page: {sorted(overlap)}")


# ---------------------------------------------------------------------------
# Stem family (SETREC-08): page-owned via performance-ipc (Web Audio path).
# ---------------------------------------------------------------------------

STEM_PARTS: Final[tuple[str, ...]] = ("vocal", "instrumental", "drums", "bass", "other")


def stem_gain_id(deck: int, part: str) -> str:
    if deck not in range(1, 5) or part not in STEM_PARTS:
        raise ValueError(f"stem gain id needs deck 1..4 and part in {STEM_PARTS}, got {deck!r} {part!r}")
    return f"deck.{deck}.stem.{part}.gain"


def stem_mute_id(deck: int, part: str) -> str:
    if deck not in range(1, 5) or part not in STEM_PARTS:
        raise ValueError(f"stem mute id needs deck 1..4 and part in {STEM_PARTS}, got {deck!r} {part!r}")
    return f"deck.{deck}.stem.{part}.mute"


def stem_solo_id(deck: int, part: str) -> str:
    if deck not in range(1, 5) or part not in STEM_PARTS:
        raise ValueError(f"stem solo id needs deck 1..4 and part in {STEM_PARTS}, got {deck!r} {part!r}")
    return f"deck.{deck}.stem.{part}.solo"


STEM_CONTINUOUS_IDS: Final[frozenset[str]] = frozenset(stem_gain_id(d, p) for d in range(1, 5) for p in STEM_PARTS)

STEM_SET_IDS: Final[frozenset[str]] = frozenset(
    {stem_gain_id(d, p) for d in range(1, 5) for p in STEM_PARTS}
    | {stem_mute_id(d, p) for d in range(1, 5) for p in STEM_PARTS}
    | {stem_solo_id(d, p) for d in range(1, 5) for p in STEM_PARTS}
)

SCRIPTED_STEM_CONTROL_IDS: Final[tuple[str, ...]] = tuple(
    id_ for d, p in ((1, part) for part in STEM_PARTS) for id_ in (stem_mute_id(1, p), stem_gain_id(1, p))
) + (
    stem_solo_id(1, "vocal"),
    stem_mute_id(2, "vocal"),
    stem_gain_id(2, "instrumental"),
)


def render_typescript() -> str:
    lines = [
        "// GENERATED from apps/sets/control_ids.py by `uv run python -m apps.sets.control_ids --write`.",
        "// Do not edit: change the Python registry and regenerate.",
        "",
        "export const CONTROL_EVENT_VERSION = 2 as const;",
        f"export const CONTROL_COALESCE_QUANTUM_MS = {COALESCE_QUANTUM_MS};",
        "",
        "export type ControlKind = 'set' | 'trigger';",
        "export type ControlSource = 'user' | 'controller' | 'agent' | 'replay';",
        "",
        "export const TRANSPORT_TEMPO_CONTROL_TEMPLATES = [",
    ]
    for index, spec in enumerate(TRANSPORT_TEMPO_CONTROLS):
        comma = "," if index < len(TRANSPORT_TEMPO_CONTROLS) - 1 else ""
        lines.append(f"\t'{spec.id}'{comma}")
    lines.append("] as const;")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Regenerate control id TS constants.")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    target = _REPO_ROOT / GENERATED_TS_REL
    rendered = render_typescript()
    if args.write:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered, encoding="utf-8")
        print(f"[OK] wrote {GENERATED_TS_REL}")
        return 0
    if not target.exists() or target.read_text(encoding="utf-8") != rendered:
        print(f"[ERROR] {GENERATED_TS_REL} is stale: run with --write", flush=True)
        return 1
    print(f"[OK] {GENERATED_TS_REL} matches the registry")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
