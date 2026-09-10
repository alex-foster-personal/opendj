"""Loudness lane scorer: pyloudnorm LUFS and a 4x-oversampled true-peak ruler.

THE SCORER IS THE RULER, NOT THE PRODUCER. Candidates emit numbers
(`integrated_lufs`, `true_peak_dbtp`, optional `rms_db`); this module compares
them to two independent references written into the bundle's truth file. It
must not import `apps.loudness.scan` or `apps.analysis_loudness` -- those are
the candidate. `apps/analysis_loudness/round0.py` both measures and gates in
one process; splitting that is the whole point of this harness.

LUFS REFERENCE is the `lufs_pyloudnorm` field, filled by
`scripts/loudnessbench/pyloudnorm_ref.py` (PEP 723, never imported here) on a
real bundle, or handwritten on a synthetic one. This module never imports
pyloudnorm: scipy is a project dep so `_true_peak` can live in the
pytest-imported scorer, pyloudnorm cannot.

dBTP REFERENCE is `_true_peak` below, ported from
`.planning/legacy-app-audits/pn_measure.py:117-118` and re-homed here as spec
section 2 requires. It is not the candidate (ffmpeg `ebur128=peak=true`). It
is not pyloudnorm, which has no true peak. Spec section 8: "if the dBTP gate
compares against pyloudnorm (which has no true peak) then broken". This
function is the only legal dBTP comparison target. There is no 1x / sample-peak
fallback: the acceptance test exists specifically to go red if the oversampling
factor `4` becomes a `1`.

RMS vs MIK ZVOLUME is a reported-only control, never a gate. Spec section 8:
"if the loudness gate compares LUFS against RMS dB or against a rekordbox
column then broken". Trap fields such as `sample_peak_db` or `rekordbox_lufs`
may appear in synthetic truth; this scorer does not read them.

OMITTED / FAILED FIXTURES stay in the denominator and score as outside both
0.1 gates (honest denominators, same refusal as key_lane / beatgrid_lane).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
from scipy import signal

__all__ = [
    "LUFS_TOLERANCE_LU",
    "SCORER_VERSION",
    "TRUE_PEAK_TOLERANCE_DB",
    "load_bundle",
    "render_table",
    "score_bundle",
]

SCORER_VERSION = "1.0.0"

LUFS_TOLERANCE_LU = 0.1
TRUE_PEAK_TOLERANCE_DB = 0.1

_ROLE_ORDER = {"negative_control": 0, "positive_control": 1}

_OMITTED_DELTA = math.inf

_TruthRow = dict[str, float | None]


def _db(v: float) -> float:
    return -np.inf if v <= 0 else 20.0 * np.log10(v)


def _true_peak(x: np.ndarray, sr: int) -> float:
    """4x-oversampled peak. BS.1770-4 requires >= 4x for true-peak metering.

    `sr` is unused in the `pn_measure.py:117-118` source and stays in the
    signature so a later reader diffing against that port sees the original,
    not a rewrite. The oversampling factor is the constant 4, never 1.
    """
    _ = sr
    up = signal.resample_poly(x, 4, 1, axis=0)
    return _db(float(np.abs(up).max()))


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    return None


def _refuse_malformed_join(
    bundle: Path, truth_name: str, truth: dict[str, Any], stable_ids: list[str]
) -> None:
    """Refuse a bundle whose manifest/truth join is broken, rather than let
    it score as a silently shrunken denominator (same refusal as key_lane).
    """
    duplicates = sorted({sid for sid in stable_ids if stable_ids.count(sid) > 1})
    if duplicates:
        raise ValueError(
            f"{bundle}/manifest.json lists {len(duplicates)} duplicate fixture "
            f"stable_id(s), first {duplicates[0]!r}: a malformed join must not "
            "silently collapse into a smaller scored set."
        )
    missing = sorted(sid for sid in stable_ids if sid not in truth)
    if missing:
        raise ValueError(
            f"{bundle}/{truth_name} has no truth row at all for {len(missing)} "
            f"fixture(s) the manifest declares, first {missing[0]!r}: a broken "
            "join must not silently become a smaller denominator."
        )


def load_bundle(bundle: Path) -> tuple[dict[str, Any], dict[str, _TruthRow]]:
    """`(manifest, loudness truth by stable_id)`.

    Public because `controls.py` needs the same pyloudnorm LUFS and 4x dBTP
    the `truth_echo` control answers with. Only `lufs_pyloudnorm`, `dbtp_4x`
    and optional `mik_zvolume` are read; trap fields are ignored.
    """
    bundle = Path(bundle)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    reads = manifest.get("reads") or {}
    truth_name = reads.get("truth")
    if not truth_name:
        raise ValueError(
            f"{bundle}/manifest.json declares no reads.truth for the loudness lane"
        )
    raw = json.loads((bundle / truth_name).read_text(encoding="utf-8"))["loudness"]
    stable_ids = [row["stable_id"] for row in manifest.get("fixtures") or []]
    _refuse_malformed_join(bundle, truth_name, raw, stable_ids)
    parsed: dict[str, _TruthRow] = {}
    for sid in stable_ids:
        row = raw[sid]
        lufs = _as_float(row.get("lufs_pyloudnorm"))
        dbtp = _as_float(row.get("dbtp_4x"))
        if lufs is None or dbtp is None:
            raise ValueError(
                f"{bundle}/{truth_name} fixture {sid!r} is missing a numeric "
                "lufs_pyloudnorm or dbtp_4x; both are required truth fields."
            )
        parsed[sid] = {
            "lufs_pyloudnorm": lufs,
            "dbtp_4x": dbtp,
            "mik_zvolume": _as_float(row.get("mik_zvolume")),
        }
    return manifest, parsed


def _refuse_unknown(results: dict[str, Any], stable_ids: list[str]) -> None:
    unknown = sorted(set(results) - set(stable_ids))
    if unknown:
        raise ValueError(
            f"this arm answered {len(unknown)} fixture(s) the bundle does not contain, "
            f"first {unknown[0]!r}. It was run against a different fixture set; "
            "scoring it here would attribute one bundle's numbers to another."
        )


def _parse_candidate(
    result: dict[str, Any] | None,
) -> tuple[float | None, float | None, float | None]:
    """`(lufs, dbtp, rms)` or Nones when the result is missing, errored, or unparseable."""
    if not result or result.get("error"):
        return None, None, None
    return (
        _as_float(result.get("integrated_lufs")),
        _as_float(result.get("true_peak_dbtp")),
        _as_float(result.get("rms_db")),
    )


def _delta_or_omitted(candidate: float | None, truth: float) -> float:
    return _OMITTED_DELTA if candidate is None else abs(candidate - truth)


def _mean(values: list[float]) -> float | None:
    return (sum(values) / len(values)) if values else None


def _max(values: list[float]) -> float | None:
    return max(values) if values else None


def _gate_cell(deltas: list[float], tolerance: float, *, lu: bool) -> dict[str, Any]:
    n = len(deltas)
    cell = {
        "n": n,
        "n_within_0_1": sum(1 for delta in deltas if delta <= tolerance),
    }
    if lu:
        cell["max_abs_delta_lu"] = _max(deltas)
        cell["mean_abs_delta_lu"] = _mean(deltas)
    else:
        cell["max_abs_delta_db"] = _max(deltas)
        cell["mean_abs_delta_db"] = _mean(deltas)
    return cell


def _score_arm(
    stable_ids: list[str], truth: dict[str, _TruthRow], results: dict[str, Any]
) -> dict[str, Any]:
    _refuse_unknown(results, stable_ids)
    lufs_deltas: list[float] = []
    dbtp_deltas: list[float] = []
    mik_deltas: list[float] = []
    n_omitted = 0
    n_failed = 0
    for sid in stable_ids:
        row = truth[sid]
        if sid not in results:
            n_omitted += 1
            lufs, dbtp, rms = None, None, None
        else:
            lufs, dbtp, rms = _parse_candidate(results.get(sid))
            if lufs is None or dbtp is None:
                n_failed += 1
                lufs, dbtp = None, None
        lufs_deltas.append(_delta_or_omitted(lufs, float(row["lufs_pyloudnorm"])))
        dbtp_deltas.append(_delta_or_omitted(dbtp, float(row["dbtp_4x"])))
        mik = row.get("mik_zvolume")
        if rms is not None and mik is not None:
            mik_deltas.append(abs(rms - float(mik)))
    n = len(stable_ids)
    n_both = sum(
        1
        for lufs_delta, dbtp_delta in zip(lufs_deltas, dbtp_deltas, strict=True)
        if lufs_delta <= LUFS_TOLERANCE_LU and dbtp_delta <= TRUE_PEAK_TOLERANCE_DB
    )
    return {
        "vs_pyloudnorm": _gate_cell(lufs_deltas, LUFS_TOLERANCE_LU, lu=True),
        "vs_true_peak_4x": _gate_cell(dbtp_deltas, TRUE_PEAK_TOLERANCE_DB, lu=False),
        "vs_mik_zvolume_reported": {
            "n": len(mik_deltas),
            "mean_abs_delta_db": _mean(mik_deltas),
        },
        "n_omitted": n_omitted,
        "n_failed": n_failed,
        "gate_pass": n > 0 and n_both == n,
    }


def score_bundle(bundle: Path, arms: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Score every arm of a round against one bundle's pyloudnorm + 4x dBTP truth."""
    manifest, truth = load_bundle(Path(bundle))
    stable_ids = list(truth)
    scored: dict[str, Any] = {}
    for name, arm in arms.items():
        results = (arm["payload"].get("results")) or {}
        scored[name] = {
            "role": arm["role"],
            "note": arm.get("note", ""),
            "producer": arm["payload"].get("candidate"),
            **_score_arm(stable_ids, truth, results),
        }
    return {
        "lane": "loudness",
        "scorer_version": SCORER_VERSION,
        "n_fixtures": len(stable_ids),
        "truth": (manifest.get("reads") or {}).get("truth", "inline"),
        "arms": scored,
    }


def _fmt(value: float | None) -> str:
    if value is None:
        return "-"
    if not math.isfinite(value):
        return "inf" if value > 0 else "-inf"
    return f"{value:.3f}"


def render_table(report: dict[str, Any]) -> str:
    """One markdown table, controls first; RMS vs MIK is labeled reported-only."""
    order = sorted(
        report["arms"].items(),
        key=lambda item: (_ROLE_ORDER.get(item[1]["role"], 2), item[0]),
    )
    header = (
        "| arm | role | LUFS vs pyloudnorm (n, within 0.1, max abs Δ LU, mean abs Δ LU) | "
        "dBTP vs 4x true-peak (n, within 0.1, max abs Δ dB, mean abs Δ dB) | "
        "RMS vs MIK (reported-only, not a gate) (n, mean abs Δ dB) | "
        "n_omitted | n_failed | gate_pass |"
    )
    delimiter = "|---|---|---|---|---|---|---|---|"
    lines = [
        f"n_fixtures (bundle denominator) = {report['n_fixtures']}",
        "",
        header,
        delimiter,
    ]
    for name, arm in order:
        lufs, dbtp, mik = (
            arm["vs_pyloudnorm"],
            arm["vs_true_peak_4x"],
            arm["vs_mik_zvolume_reported"],
        )
        lines.append(
            f"| {name} | {arm['role']} | "
            f"{lufs['n']}, {lufs['n_within_0_1']}, "
            f"{_fmt(lufs['max_abs_delta_lu'])}, {_fmt(lufs['mean_abs_delta_lu'])} | "
            f"{dbtp['n']}, {dbtp['n_within_0_1']}, "
            f"{_fmt(dbtp['max_abs_delta_db'])}, {_fmt(dbtp['mean_abs_delta_db'])} | "
            f"{mik['n']}, {_fmt(mik['mean_abs_delta_db'])} | "
            f"{arm['n_omitted']} | {arm['n_failed']} | {arm['gate_pass']} |"
        )
    return "\n".join(lines)
