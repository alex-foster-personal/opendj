"""Blind A/B widget generator for lighting show variants (issue #9905, spec item L64).

Writes ONE self-contained, offline HTML page for blind A/B calibration sessions: an audio
excerpt (WAV or MP3), 2 to 4 show variants (frames JSON files, the L73 offline render output),
a pair count and a seed.

Usage:
    python -m scripts.lighting_ab_widget --audio excerpt.wav \
        --variant cool=cool.frames.json --variant warm=warm.frames.json \
        --pairs 24 --seed 1 --out ab.html

Page: two 2D stage canvases (A left, B right), a waveform player with click-to-seek that drives
both canvases in sync, seeded randomised pair and side order, keys A / B / N (no preference) with
matching buttons, and an Export button that downloads the ratings as JSON. Variant ids appear only
in that export; the page payload is base64 JSON so view-source does not unblind the session. A CSP
meta tag (default-src 'none') blocks every network load.

Frames file shape (provisional until #9904 / L73 fixes its schema):
    {"frames": [...]} or a bare list of frames. Each frame: int "t_ns" (strictly increasing) and a
    "fixtures" list. Each fixture: "id" non-empty str, "color" [r, g, b] ints 0..255, "intensity"
    number 0..1; optional "x", "y" numbers 0..1, "strobe" bool, "pan", "tilt" numbers (degrees).
    Unknown keys are ignored.
Time base: audio t = 0 maps to frames[0].t_ns, so a render from --from-s 60 lines up with an
excerpt cut at 60 s.

Requirements:
    ✔︎ ✅ 🎯 generator writes one offline HTML file
        [if] the page contains an http(s) URL, a <link> or fetch( [then] ⛔️
    ✔︎ ✅ 🎯 waveform with click-to-seek and two stage canvases
        [if] the parsed page lacks #waveform, its click handler or two canvas.stage [then] ⛔️
    ✔︎ ✅ 🎯 deterministic pair order
        [if] the same seed gives a different pair list or page bytes [then] ⛔️
    ✔︎ ✅ 🎯 fail fast on bad input
        [if] a frames file with intensity 1.4 is accepted [then] ⛔️

-Claude
"""

from __future__ import annotations

import argparse
import base64
import html
import itertools
import json
import random
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TEMPLATE_PATH = Path(__file__).with_name("lighting_ab_widget_template.html")
PAYLOAD_TOKEN = "/*__AB_PAYLOAD__*/null"
TITLE_TOKEN = "__AB_TITLE__"
AUDIO_MIME = {".wav": "audio/wav", ".mp3": "audio/mpeg"}
MIN_VARIANTS, MAX_VARIANTS = 2, 4
VARIANT_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
DEFAULT_TITLE = "Lighting blind A/B"


class WidgetInputError(ValueError):
    """Bad generator input; main reports it as [ERROR] and exits 2."""


@dataclass(frozen=True)
class Pair:
    index: int
    left: int
    right: int


# ----- input loading -----------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_audio_magic(path: Path, data: bytes) -> None:
    suffix = path.suffix.lower()
    if suffix == ".wav":
        ok = data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    elif suffix == ".mp3":
        ok = data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0)
    else:
        raise WidgetInputError(f"{path}: audio must be .wav or .mp3, got {path.suffix!r}")
    if not ok:
        raise WidgetInputError(f"{path}: file content is not a valid {suffix} file")


def load_audio_data_url(path: Path) -> str:
    if path.suffix.lower() not in AUDIO_MIME:
        raise WidgetInputError(f"{path}: audio must be .wav or .mp3, got {path.suffix!r}")
    data = path.read_bytes()
    if not data:
        raise WidgetInputError(f"{path}: audio file is empty")
    _check_audio_magic(path, data)
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:{AUDIO_MIME[path.suffix.lower()]};base64,{encoded}"


def _validate_fixture(path: Path, where: str, fixture: Any) -> None:
    if not isinstance(fixture, dict):
        raise WidgetInputError(f"{path}: {where} must be an object")
    if not isinstance(fixture.get("id"), str) or not fixture["id"]:
        raise WidgetInputError(f"{path}: {where}.id must be a non-empty string")
    color = fixture.get("color")
    if not (
        isinstance(color, list)
        and len(color) == 3
        and all(isinstance(c, int) and not isinstance(c, bool) and 0 <= c <= 255 for c in color)
    ):
        raise WidgetInputError(f"{path}: {where}.color must be 3 ints in 0..255, got {color!r}")
    intensity: Any = fixture.get("intensity")
    if not (_is_number(intensity) and 0 <= intensity <= 1):
        raise WidgetInputError(f"{path}: {where}.intensity must be a number in 0..1, got {intensity!r}")
    for key in ("x", "y"):
        if key in fixture and not (_is_number(fixture[key]) and 0 <= fixture[key] <= 1):
            raise WidgetInputError(f"{path}: {where}.{key} must be a number in 0..1, got {fixture[key]!r}")
    for key in ("pan", "tilt"):
        if key in fixture and not _is_number(fixture[key]):
            raise WidgetInputError(f"{path}: {where}.{key} must be a number, got {fixture[key]!r}")
    if "strobe" in fixture and not isinstance(fixture["strobe"], bool):
        raise WidgetInputError(f"{path}: {where}.strobe must be a bool, got {fixture['strobe']!r}")


def load_frames(path: Path) -> list[dict[str, Any]]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(doc, dict) and isinstance(doc.get("frames"), list):
        frames = doc["frames"]
    elif isinstance(doc, list):
        frames = doc
    else:
        raise WidgetInputError(f"{path}: top level must be a frames list or an object with a frames list")
    if not frames:
        raise WidgetInputError(f"{path}: frames must be non-empty")
    previous_t: int | None = None
    for i, frame in enumerate(frames):
        if not isinstance(frame, dict):
            raise WidgetInputError(f"{path}: frames[{i}] must be an object")
        t_ns = frame.get("t_ns")
        if not isinstance(t_ns, int) or isinstance(t_ns, bool):
            raise WidgetInputError(f"{path}: frames[{i}].t_ns must be an int, got {t_ns!r}")
        if previous_t is not None and t_ns <= previous_t:
            raise WidgetInputError(f"{path}: frames[{i}].t_ns must be strictly increasing, got {t_ns}")
        previous_t = t_ns
        fixtures = frame.get("fixtures")
        if not isinstance(fixtures, list):
            raise WidgetInputError(f"{path}: frames[{i}].fixtures must be a list")
        for j, fixture in enumerate(fixtures):
            _validate_fixture(path, f"frames[{i}].fixtures[{j}]", fixture)
    return frames


def parse_variant_arg(arg: str) -> tuple[str, Path]:
    if "=" in arg:
        variant_id, raw_path = arg.split("=", 1)
        path = Path(raw_path)
    else:
        path = Path(arg)
        variant_id = path.stem.removesuffix(".frames")
    if not VARIANT_ID_RE.match(variant_id):
        raise WidgetInputError(f"variant id {variant_id!r} must match {VARIANT_ID_RE.pattern}")
    return variant_id, path


# ----- pairs and payload -------------------------------------------------------------------


def build_pairs(variant_count: int, pair_count: int, seed: int) -> list[Pair]:
    if not MIN_VARIANTS <= variant_count <= MAX_VARIANTS:
        raise WidgetInputError(f"need {MIN_VARIANTS} to {MAX_VARIANTS} variants, got {variant_count}")
    if pair_count < 1:
        raise WidgetInputError(f"pair count must be >= 1, got {pair_count}")
    rng = random.Random(seed)
    combos = list(itertools.combinations(range(variant_count), 2))
    ordered: list[tuple[int, int]] = []
    while len(ordered) < pair_count:
        block = combos[:]
        rng.shuffle(block)
        ordered.extend(block)
    pairs = []
    for index, (first, second) in enumerate(ordered[:pair_count]):
        left, right = (second, first) if rng.random() < 0.5 else (first, second)
        pairs.append(Pair(index=index, left=left, right=right))
    return pairs


def build_payload(
    audio_data_url: str,
    variant_ids: list[str],
    variant_frames: list[list[dict[str, Any]]],
    pairs: list[Pair],
    seed: int,
    audio_name: str,
) -> dict[str, Any]:
    return {
        "audio": audio_data_url,
        "audio_name": audio_name,
        "seed": seed,
        "variant_ids": variant_ids,
        "frames": variant_frames,
        "pairs": [[p.index, p.left, p.right] for p in pairs],
    }


def render_widget(
    audio_path: Path,
    variants: list[tuple[str, Path]],
    pair_count: int,
    seed: int,
    title: str = DEFAULT_TITLE,
) -> str:
    ids = [variant_id for variant_id, _ in variants]
    if len(set(ids)) != len(ids):
        raise WidgetInputError(f"variant ids must be unique, got {ids}")
    pairs = build_pairs(len(variants), pair_count, seed)
    payload = build_payload(
        load_audio_data_url(audio_path),
        ids,
        [load_frames(path) for _, path in variants],
        pairs,
        seed,
        audio_path.name,
    )
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    encoded = base64.b64encode(blob).decode("ascii")
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    if template.count(PAYLOAD_TOKEN) != 1 or TITLE_TOKEN not in template:
        raise WidgetInputError(f"{TEMPLATE_PATH}: payload token must occur once and title token at least once")
    return template.replace(PAYLOAD_TOKEN, f'"{encoded}"').replace(TITLE_TOKEN, html.escape(title))


# ----- CLI ---------------------------------------------------------------------------------


def _require_out_dir(out: Path) -> None:
    if not out.parent.is_dir():
        raise WidgetInputError(f"{out.parent}: output directory does not exist")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--variant", action="append", required=True, help="ID=PATH, 2 to 4 times")
    parser.add_argument("--pairs", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--title", default=DEFAULT_TITLE)
    args = parser.parse_args(argv)
    try:
        _require_out_dir(args.out)
        variants = [parse_variant_arg(arg) for arg in args.variant]
        page = render_widget(args.audio, variants, args.pairs, args.seed, args.title)
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    args.out.write_text(page, encoding="utf-8")
    size = len(page.encode("utf-8"))
    print(f"[OK] wrote {args.out} ({args.pairs} pairs, {len(variants)} variants, {size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
