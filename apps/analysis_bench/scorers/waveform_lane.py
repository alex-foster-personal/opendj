"""Waveform lane adapter: the existing scorer 1.0.0, behind the shared interface.

NOTHING IS RESCORED HERE. Every r still comes from
`apps.analysis_waveform.score` (SCORER_VERSION 1.0.0) through `pearson`.
This module only translates: it takes the shared `{arm name: candidate
payload}` shape the harness produces, loads the bundle's PWV3/PWV5/PWV6
truth the way `score.load_truth` already does, and hands back the report
and markdown the round log wants. Reimplementing the correlation would make
a round-over-round delta against the NATIVE-06 table unattributable.

Do not call `score.run()`. `run()` decodes audio and ignores candidate
payloads; the harness model is that the arm already answered.

NaN FROM A CONSTANT PREDICTOR IS A FLOOR OF 0.0, NOT AN UNMEASURED ROW.
`score.pearson` returns NaN when either side has no variance, and
`score._summary` then drops NaNs -- correct for an unmeasured track in the
standalone CLI, wrong for the bench negative control `constant_band` (a
flat level has no variance on purpose). A non-finite r counts as 0.0 and
stays in `n`. An omitted fixture (no result / error) also scores 0.0 on
every band and increments `n_omitted`.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from apps.analysis_waveform.decode import BAND_NAMES
from apps.analysis_waveform.score import SCORER_VERSION, load_truth, pearson

__all__ = [
    "SCORER_VERSION",
    "load_bundle",
    "load_truth",
    "pearson",
    "render_table",
    "score_bundle",
]

_ROLE_ORDER = {"negative_control": 0, "positive_control": 1}

_FLOOR_R = 0.0


def load_bundle(bundle: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """`(manifest, tracks)`. Prefers the native `manifest["tracks"]` shape
    `score.load_truth` already consumes.
    """
    bundle = Path(bundle)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    tracks = list(manifest.get("tracks") or [])
    if not tracks:
        raise ValueError(
            f"{bundle}/manifest.json declares no tracks for the waveform lane"
        )
    stable_ids = [track["stable_id"] for track in tracks]
    duplicates = sorted({sid for sid in stable_ids if stable_ids.count(sid) > 1})
    if duplicates:
        raise ValueError(
            f"{bundle}/manifest.json lists {len(duplicates)} duplicate track "
            f"stable_id(s), first {duplicates[0]!r}: a malformed join must not "
            "silently collapse into a smaller scored set."
        )
    return manifest, tracks


def _refuse_unknown(results: dict[str, Any], stable_ids: list[str]) -> None:
    unknown = sorted(set(results) - set(stable_ids))
    if unknown:
        raise ValueError(
            f"this arm answered {len(unknown)} fixture(s) the bundle does not contain, "
            f"first {unknown[0]!r}. It was run against a different fixture set; "
            "scoring it here would attribute one bundle's numbers to another."
        )


def _finite_r(value: float) -> float:
    return value if math.isfinite(value) else _FLOOR_R


def _stack_bands(result: dict[str, Any] | None) -> np.ndarray | None:
    if not result or result.get("error"):
        return None
    bands = result.get("bands")
    if not isinstance(bands, dict):
        return None
    try:
        return np.stack(
            [np.asarray(bands[name], dtype=np.float64) for name in BAND_NAMES],
            axis=1,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _summarize(values: list[float]) -> dict[str, float]:
    array = np.asarray(values, dtype=np.float64)
    return {
        "n": len(values),
        "mean": float(array.mean()),
        "median": float(np.median(array)),
        "min": float(array.min()),
    }


def _score_arm(
    bundle: Path, tracks: list[dict[str, Any]], results: dict[str, Any]
) -> dict[str, Any]:
    stable_ids = [track["stable_id"] for track in tracks]
    _refuse_unknown(results, stable_ids)
    per_band: dict[str, list[float]] = {name: [] for name in BAND_NAMES}
    n_omitted = 0
    for track in tracks:
        sid = track["stable_id"]
        ours = _stack_bands(results.get(sid))
        if ours is None:
            n_omitted += 1
            for name in BAND_NAMES:
                per_band[name].append(_FLOOR_R)
            continue
        truth = load_truth(bundle, track)
        for index, name in enumerate(BAND_NAMES):
            per_band[name].append(_finite_r(pearson(ours[:, index], truth[:, index])))
    return {
        name: _summarize(values) for name, values in per_band.items()
    } | {"n_omitted": n_omitted}


def score_bundle(bundle: Path, arms: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Score every arm of a round against one bundle's rekordbox PWV truth."""
    bundle = Path(bundle)
    _manifest, tracks = load_bundle(bundle)
    scored: dict[str, Any] = {}
    for name, arm in arms.items():
        results = (arm["payload"].get("results")) or {}
        scored[name] = {
            "role": arm["role"],
            "note": arm.get("note", ""),
            "producer": arm["payload"].get("candidate"),
            **_score_arm(bundle, tracks, results),
        }
    return {
        "lane": "waveform",
        "scorer_version": SCORER_VERSION,
        "n_fixtures": len(tracks),
        "truth": "rekordbox PWV3/PWV5/PWV6 per-band columns",
        "arms": scored,
    }


def render_table(report: dict[str, Any]) -> str:
    """One row per arm, controls first; per-band median r with its n."""
    order = sorted(
        report["arms"].items(),
        key=lambda item: (_ROLE_ORDER.get(item[1]["role"], 2), item[0]),
    )
    header = (
        "| arm | role | n_omitted | "
        "low median r (n) | mid median r (n) | high median r (n) |"
    )
    delimiter = "|---|---|---|---|---|---|"
    lines = [
        f"n_fixtures (bundle denominator) = {report['n_fixtures']}",
        "",
        header,
        delimiter,
    ]
    for name, arm in order:
        cells = " | ".join(
            f"{arm[band]['median']:.6f} ({arm[band]['n']})" for band in BAND_NAMES
        )
        lines.append(
            f"| {name} | {arm['role']} | {arm['n_omitted']} | {cells} |"
        )
    return "\n".join(lines)
