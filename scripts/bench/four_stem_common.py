"""Shared, dependency-free logic for the four-stem model bake-off.

Both runners need the same arm list, the same gate reading and the same paired
statistic, and they run in DIFFERENT environments: the CPU runner is a PEP 723
script that pulls torch, the GPU runner executes inside the repo venv next to
modal. Anything they share therefore has to import with nothing but the standard
library, which is why this module is separate rather than living in either one.

Two runners disagreeing about which model is the default, or about which cells
are gated, would be a silent confound rather than a visible error.

-Claude
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

STEMS: tuple[str, ...] = ("vocals", "drums", "bass", "other")

# The three tiers' models (apps/stems/tiers.py): LOCAL=htdemucs, M=hdemucs_mmi,
# L=htdemucs_ft. hdemucs_mmi is the incumbent default and is therefore the
# reference every paired delta is taken against.
DEFAULT_MODEL: str = "hdemucs_mmi"
MODELS: tuple[str, ...] = ("hdemucs_mmi", "htdemucs", "htdemucs_ft")
OVERLAP: float = 0.25
SHIFTS: int = 0

# This project's audibility bar. A delta under it is not a reason to change
# anything, and one over it must not be hidden.
AUDIBILITY_DB: float = 0.2


def gate_flags(*gate_jsons: Path) -> dict[str, dict[str, Any]]:
    """Per (track, stem) pass/fail, READ from the gate's own verdict files.

    Read, not recomputed: the gate chose these windows before any model score
    existed, and re-deriving the flags here would let a runner move a bar after
    seeing a result.

    Several files are accepted because the sample was widened in rounds, and a
    round-2 track's verdict lives in its own file. A track appearing twice is an
    error rather than a last-one-wins merge: two gate rows for one track means
    two different windows, and silently picking one would decide which window a
    score belongs to by file order.
    """
    out: dict[str, dict[str, Any]] = {}
    for gate_json in gate_jsons:
        path = Path(gate_json)
        if not path.is_file():
            continue
        for candidate in json.loads(path.read_text())["candidates"]:
            if candidate["track"] in out:
                raise RuntimeError(
                    f"{candidate['track']} has a gate row in more than one file; "
                    "refusing to guess which window its scores belong to")
            _add_flag(out, candidate)
    return out


def _add_flag(out: dict[str, dict[str, Any]], candidate: dict[str, Any]) -> None:
    failures = candidate["failures"]
    out[candidate["track"]] = {
        "window_start_s": candidate["start_s"],
        "window_length_s": candidate["length_s"],
        "track_passed": candidate["passed"],
        "stem_passed": {s: s not in failures for s in STEMS},
        "stem_failures": {s: failures.get(s, []) for s in STEMS},
        "mixture_floor_si_sdr": candidate["mixture_floor_si_sdr"],
        "stem_energy_share": {s["stem"]: s["energy_share"] for s in candidate["stems"]},
        "stem_spectral_centroid_hz": {
            s["stem"]: s["spectral_centroid_hz"] for s in candidate["stems"]},
    }


def paired_deltas(cube: dict[str, dict[str, dict[str, float]]],
                  flags: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Per (model, stem) paired delta against DEFAULT_MODEL, gated cells only.

    PAIRED because every model separated the identical window, so track
    difficulty -- which spans roughly 15 dB across this sample, far more than any
    gap between models -- cancels. An unpaired difference of medians would be
    dominated by that spread and can reverse the per-track sign.

    Ungated cells are carried in ``per_track_ungated`` rather than dropped: a
    stem buried in its own window scores toward the mixture floor, so its delta
    is not evidence, but hiding it would be a silent scope cut.
    """
    rows: list[dict[str, Any]] = []
    for model in MODELS:
        if model == DEFAULT_MODEL:
            continue
        for stem in STEMS:
            gated: dict[str, float] = {}
            ungated: dict[str, float] = {}
            for track in sorted(cube):
                if model not in cube[track] or DEFAULT_MODEL not in cube[track]:
                    raise RuntimeError(f"{track} is missing a model, the cube has a hole")
                delta = round(cube[track][model][stem] - cube[track][DEFAULT_MODEL][stem], 3)
                target = gated if flags[track]["stem_passed"][stem] else ungated
                target[track] = delta
            values = list(gated.values())
            if not values:
                raise RuntimeError(f"no gated track for stem {stem}, cannot conclude")
            mean = statistics.fmean(values)
            sd = statistics.stdev(values) if len(values) > 1 else 0.0
            rows.append({
                "model": model,
                "vs": DEFAULT_MODEL,
                "stem": stem,
                "n_gated": len(values),
                "mean_delta_db": round(mean, 3),
                "median_delta_db": round(statistics.median(values), 3),
                "sd": round(sd, 3),
                # t against zero. n is small on purpose; read this as a
                # magnitude-versus-spread check, not a significance claim.
                "t": round(mean / (sd / len(values) ** 0.5), 2) if sd > 0 else None,
                "min_delta_db": round(min(values), 3),
                "max_delta_db": round(max(values), 3),
                "tracks_favouring_challenger": sum(1 for v in values if v > 0),
                "beats_audibility_bar": abs(mean) >= AUDIBILITY_DB,
                "per_track_gated": gated,
                "per_track_ungated": ungated,
            })
    return rows
