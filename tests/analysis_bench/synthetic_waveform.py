"""A synthetic waveform bundle, so the harness can be proved without PWV.

No audio is decoded here -- the waveform lane's controls emit preview-aligned
`bands` already in 0..1, and the adapter compares them to PWV6-shaped truth
JSON `score.load_truth` already parses. NOT a substitute for a real rekordbox
PWV bundle (`tests/analysis_bench/synthetic.py`'s beatgrid counterpart makes
the same disclaimer): it exists to exercise the adapter, the constant_band
floor, the truth_echo ceiling, and the round log end to end on any host.

Two tracks, three non-constant preview columns each (length 32, a ramp plus
a seeded sequence, not self-similar under time reversal so an accidental
call of `score.run` internals cannot sneak a pass).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from apps.analysis_waveform.decode import BAND_NAMES

SCALE = 127.0
N_COLUMNS = 32
TAG = "PWV6"

TRACK_IDS = ("synthetic-ramp", "synthetic-seeded")


def _bands(kind: str) -> dict[str, list[float]]:
    t = np.arange(N_COLUMNS, dtype=np.float64)
    if kind == "ramp":
        low = np.clip(8.0 + 3.0 * t, 0.0, SCALE)
        mid = np.clip(20.0 + 1.7 * t + 8.0 * np.sin(t / 5.0), 0.0, SCALE)
        high = np.clip(4.0 + (t ** 1.3), 0.0, SCALE)
    else:
        rng = np.random.default_rng(20260910)
        noise = rng.normal(0.0, 2.0, N_COLUMNS)
        low = np.clip(15.0 + 2.2 * t + noise, 0.0, SCALE)
        mid = np.clip(40.0 + 12.0 * np.sin(t / 3.0) + 0.8 * t, 0.0, SCALE)
        high = np.clip(90.0 - 2.5 * t, 0.0, SCALE)
    return {"low": low.tolist(), "mid": mid.tolist(), "high": high.tolist()}


def build(staged: Path) -> list[dict]:
    """Stage a waveform bundle-shaped fixture set and return its track rows."""
    staged.mkdir(parents=True, exist_ok=True)
    truth_dir = staged / "truth"
    truth_dir.mkdir(parents=True, exist_ok=True)
    tracks = []
    for stable_id, kind in zip(TRACK_IDS, ("ramp", "seeded"), strict=True):
        bands = _bands(kind)
        assert set(bands) == set(BAND_NAMES)
        payload = {
            "stable_id": stable_id,
            "tag": TAG,
            "scale": SCALE,
            "columns": N_COLUMNS,
            "bands": bands,
        }
        (truth_dir / f"{stable_id}.json").write_text(
            json.dumps(payload, indent=1), encoding="utf-8"
        )
        tracks.append(
            {
                "stable_id": stable_id,
                "truth_file": f"truth/{stable_id}.json",
                "truth_columns": N_COLUMNS,
            }
        )
    return tracks


def manifest_extra(tracks: list[dict]) -> dict:
    """The fixture description the sealed manifest has to carry for the adapter.

    `tracks[]` is the native `scripts/build_waveform_bundle.py` shape
    `score.load_truth` already consumes, so the tests exercise that loader.
    """
    return {
        "paths_relative_to": "manifest",
        "truth_tag": TAG,
        "truth_scale": SCALE,
        "synthetic": (
            "hand-built PWV6-shaped preview columns, NOT a substitute for a real "
            "rekordbox PWV bundle"
        ),
        "tracks": tracks,
    }
