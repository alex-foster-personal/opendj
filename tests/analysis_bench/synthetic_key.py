"""A synthetic key bundle, so the harness can be proved without a real
rekordbox/MIK library.

No audio and no analyzer are exercised here -- the key lane's candidates and
controls consume the reference NOTATIONS only, never the audio bytes -- so
this bundle has one payload file (`key-truth.json`) and no `wav/` directory.
NOT a substitute for a real library measurement (`tests/analysis_bench/
synthetic.py`'s beatgrid counterpart makes the same disclaimer): it exists to
exercise the harness, the bundle store, the scorer's honest-denominator
handling and the round log end to end on any host.

Six fixtures, each chosen and hand-verified (see the PR for the `uv run`
transcript cross-checking every value against `apps.analysis_key.canon`) to
exercise one documented path each:
  synthetic-agree-1, synthetic-agree-2 : rekordbox and MIK canonicalize to the
    same (pitch_class, is_minor) -> the AGREE bucket.
  synthetic-disagree-1, synthetic-disagree-2 : both references present but
    disagree -> the DISAGREE-RELATED / DISAGREE-UNRELATED split, decided per
    arm by whichever key that arm answers.
  synthetic-no-mik : rekordbox present, MIK absent -> counted in
    `n_no_reference_pair`, scorable `vs_rekordbox` only.
  synthetic-no-rekordbox : MIK present, rekordbox absent -> the mirror case,
    scorable `vs_mik` only.
"""

from __future__ import annotations

import json
from pathlib import Path

# stable_id -> (rekordbox djmdKey-style value, MIK Camelot value or None)
ROWS: dict[str, tuple[str | None, str | None]] = {
    "synthetic-agree-1": ("Am", "8A"),
    "synthetic-agree-2": ("E", "12B"),
    "synthetic-disagree-1": ("C", "7A"),
    "synthetic-disagree-2": ("F#", "3B"),
    "synthetic-no-mik": ("Gm", None),
    "synthetic-no-rekordbox": (None, "5A"),
}


def build(staged: Path) -> list[dict]:
    """Stage a key bundle-shaped fixture set and return its fixture rows."""
    staged.mkdir(parents=True, exist_ok=True)
    keys = {
        stable_id: {"rekordbox": rekordbox, "mik_camelot": mik_camelot}
        for stable_id, (rekordbox, mik_camelot) in ROWS.items()
    }
    (staged / "key-truth.json").write_text(
        json.dumps({"schema": 1, "keys": keys}, indent=1), encoding="utf-8"
    )
    return [{"stable_id": stable_id} for stable_id in ROWS]


def manifest_extra(fixtures: list[dict]) -> dict:
    """The fixture description the sealed manifest has to carry for the scorer."""
    return {
        "paths_relative_to": "manifest",
        "reads": {
            "truth": "key-truth.json",
            "checksums": "SHA256SUMS",
            "scorer": "apps/analysis_bench/scorers/key_lane.py (version stamped in every artifact)",
        },
        "synthetic": "hand-built truth pairs, NOT a substitute for a real rekordbox/MIK bundle",
        "fixtures": fixtures,
    }
