"""A synthetic loudness bundle, so the harness can be proved without a corpus.

No audio and no analyzer are exercised here -- the loudness lane's candidates
and controls consume the reference NUMBERS only, never the audio bytes -- so
this bundle has one payload file (`loudness-truth.json`) and no `wav/`
directory. NOT a substitute for a real 30-file MIK corpus (`tests/analysis_bench/
synthetic.py`'s beatgrid counterpart makes the same disclaimer): it exists to
exercise the gates, the RMS-reported-only control, the inter-sample-peak
bundle path, and the round log end to end on any host.

Two fixtures:
  synthetic-ok : EBU-ish LUFS, a sample peak that equals the 4x true peak,
    plus trap fields (`sample_peak_db`, `rekordbox_lufs`) the scorer must
    ignore, and a MIK ZVOLUME for the reported-only RMS column.
  synthetic-isp : true peak exceeds sample peak by more than 0.1 dB, no
    MIK value (omitted from the RMS denominator, not a gate failure), plus
    a `pyloudnorm_peak` trap the dBTP gate must not read.
"""

from __future__ import annotations

import json
from pathlib import Path

# stable_id -> truth row. Trap fields are intentional.
ROWS: dict[str, dict[str, float]] = {
    "synthetic-ok": {
        "lufs_pyloudnorm": -23.0,
        "dbtp_4x": -6.0,
        "mik_zvolume": -11.5,
        "sample_peak_db": -6.0,
        "rekordbox_lufs": -14.0,
    },
    "synthetic-isp": {
        "lufs_pyloudnorm": -20.0,
        "dbtp_4x": 0.30,
        "sample_peak_db": -0.10,
        "pyloudnorm_peak": -99.0,
    },
}


def build(staged: Path) -> list[dict]:
    """Stage a loudness bundle-shaped fixture set and return its fixture rows."""
    staged.mkdir(parents=True, exist_ok=True)
    (staged / "loudness-truth.json").write_text(
        json.dumps({"schema": 1, "loudness": ROWS}, indent=1), encoding="utf-8"
    )
    return [{"stable_id": stable_id} for stable_id in ROWS]


def manifest_extra(fixtures: list[dict]) -> dict:
    """The fixture description the sealed manifest has to carry for the scorer."""
    return {
        "paths_relative_to": "manifest",
        "reads": {
            "truth": "loudness-truth.json",
            "checksums": "SHA256SUMS",
            "scorer": (
                "apps/analysis_bench/scorers/loudness_lane.py "
                "(version stamped in every artifact)"
            ),
        },
        "synthetic": (
            "hand-built LUFS/dBTP pairs, NOT a substitute for a real pyloudnorm corpus"
        ),
        "fixtures": fixtures,
    }
