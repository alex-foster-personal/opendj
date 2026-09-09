"""A synthetic beatgrid bundle, so the harness can be proved without a library.

The real beatgrid fixtures are 45 second excerpts plus rekordbox ANLZ grids and
can only be built on the Mac that holds both. This builds the same SHAPE from a
click grid: enough for the harness, the bundle store, the scorer plumbing and
the round log to be exercised end to end on any host, and NOT a substitute for
the real bundle -- no audio is analyzed here, so nothing about an ANALYZER can
be measured from it.
"""

from __future__ import annotations

import json
import math
import struct
import wave
from pathlib import Path

SAMPLE_RATE = 44100
WINDOW_S = 12.0
GUARD_S = 2.0


def _click_wav(path: Path, bpm: float) -> None:
    """A metronome the length of the window, so a candidate has real audio to open."""
    period = 60.0 / bpm
    frames = bytearray()
    for index in range(int(SAMPLE_RATE * WINDOW_S)):
        t = index / SAMPLE_RATE
        phase = t % period
        value = math.sin(2 * math.pi * 1000 * phase) * math.exp(-phase * 60)
        frames += struct.pack("<h", int(max(-1.0, min(1.0, value)) * 24000))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(bytes(frames))


def build(staged: Path, bpms: tuple[float, ...] = (120.0, 128.0, 174.0)) -> list[dict]:
    """Stage a bundle-shaped fixture set and return its fixture rows to seal with."""
    (staged / "wav").mkdir(parents=True, exist_ok=True)
    fixtures = []
    truth: dict[str, list] = {}
    for index, bpm in enumerate(bpms):
        stable_id = f"synthetic-{index}"
        wav = staged / "wav" / f"{stable_id}.wav"
        _click_wav(wav, bpm)
        period = 60.0 / bpm
        beats = [
            [(n % 4) + 1, round(n * period, 5), bpm]
            for n in range(int(WINDOW_S / period))
        ]
        truth[stable_id] = beats
        fixtures.append(
            {
                "stable_id": stable_id,
                "wav": f"wav/{stable_id}.wav",
                "window_start_s": 0.0,
                "window_end_s": WINDOW_S,
                "score_start_s": GUARD_S,
                "score_end_s": WINDOW_S - GUARD_S,
                "rb_bpm": bpm,
                "is_dynamic": False,
            }
        )
    (staged / "rekordbox-truth.json").write_text(
        json.dumps({"schema": 2, "beats": truth}, indent=1), encoding="utf-8"
    )
    return fixtures


def manifest_extra(fixtures: list[dict]) -> dict:
    """The fixture description the sealed manifest has to carry for the scorer."""
    return {
        "paths_relative_to": "manifest",
        # The same three keys the real packer writes (`scripts/beatbench/bundle.py`),
        # so the synthetic bundle cannot pass a check that a real one would fail:
        # only `truth` is a payload a consumer opens.
        "reads": {
            "truth": "rekordbox-truth.json",
            "checksums": "SHA256SUMS",
            "scorer": "apps/analysis_bench/scorers/beatgrid.py (version stamped in every artifact)",
        },
        "synthetic": "click grid, NOT a substitute for a real rekordbox bundle",
        "fixtures": fixtures,
    }
