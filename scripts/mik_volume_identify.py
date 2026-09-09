#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy>=1.26"]
# ///
"""Identify empirically what MIK's undocumented ``ZSONG.ZVOLUME`` actually is.

The equivalence suite declares MIK loudness with unit ``unknown_db_family`` and
therefore cannot reach any verdict but UNTESTED. That is correct but it is also
a dead end: the field is blocked by a documentation gap, not by a measurement
failure. So measure it.

For every MIK row whose audio EXISTS on disk (see
``scripts/mik_audio_resolve.py`` for the resolution tiers) this computes five
candidate quantities, all with ffmpeg 8.x:

=========================  ==================================================
candidate                  how
=========================  ==================================================
sample-peak dBFS           ``astats`` Peak level dB
RMS dB                     ``astats`` RMS level dB (whole-file overall)
true peak dBTP             ``loudnorm`` ``input_tp`` (4x oversampled)
integrated LUFS            ``loudnorm`` ``input_i`` (EBU R128, gated)
loudness range LU          ``loudnorm`` ``input_lra``
=========================  ==================================================

then correlates each against ZVOLUME: Pearson r, the fitted constant offset
(mean of ZVOLUME minus candidate), the mean absolute error AROUND that fitted
offset, and the residual spread. The winner is the candidate with r near 1.0 AND
a near-constant offset. A high r with a wandering offset is a different
quantity that merely covaries, which is exactly the trap this is here to avoid.

sox is NOT installed on this machine, so nothing here depends on it. numpy does
the statistics and also an INDEPENDENT peak/RMS recomputation on a sample of
files, so ffmpeg's own numbers are not the only witness to themselves.

Run::

    scripts/mik_volume_identify.py --data-dir /path/to/data --workers 10
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mik_audio_resolve import ResolvedAudio, resolve, tier_counts

_PEAK = re.compile(
    r"^\s*\[Parsed_astats.*?\] Peak level dB: (-?\d+\.?\d*|-inf)", re.MULTILINE
)
_RMS = re.compile(
    r"^\s*\[Parsed_astats.*?\] RMS level dB: (-?\d+\.?\d*|-inf)", re.MULTILINE
)
_JSON_BLOCK = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.DOTALL)

CANDIDATES: tuple[str, ...] = (
    "sample_peak_dbfs",
    "true_peak_dbtp",
    "rms_db",
    "integrated_lufs",
    "loudness_range_lu",
)


@dataclass(frozen=True)
class AudioLoudness:
    """Every candidate quantity for one file. ``None`` only if ffmpeg failed."""

    mik_pk: int
    path: str
    tier: str
    volume: float
    analysis_date_iso: str | None
    reencoded_after_analysis: bool
    sample_peak_dbfs: float | None
    true_peak_dbtp: float | None
    rms_db: float | None
    integrated_lufs: float | None
    loudness_range_lu: float | None
    error: str | None = None


def _float(text: str) -> float:
    return -math.inf if text == "-inf" else float(text)


def measure(path: str) -> dict[str, float | str | None]:
    """One full-decode ffmpeg pass yielding all five candidates."""
    proc = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-nostats",
            "-i",
            path,
            "-map",
            "0:a:0",
            "-af",
            (
                "astats=measure_perchannel=none:measure_overall="
                "Peak_level+RMS_level,loudnorm=print_format=json"
            ),
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    stderr = proc.stderr
    peak = _PEAK.search(stderr)
    rms = _RMS.search(stderr)
    block = _JSON_BLOCK.search(stderr)
    if proc.returncode != 0 or peak is None or rms is None or block is None:
        tail = " | ".join(line for line in stderr.strip().splitlines()[-3:])
        return {"error": f"ffmpeg rc={proc.returncode}: {tail[:400]}"}
    stats = json.loads(block.group(0))
    return {
        "sample_peak_dbfs": _float(peak.group(1)),
        "rms_db": _float(rms.group(1)),
        "true_peak_dbtp": _float(stats["input_tp"]),
        "integrated_lufs": _float(stats["input_i"]),
        "loudness_range_lu": _float(stats["input_lra"]),
        "error": None,
    }


def _measure_row(row_dict: dict) -> dict:
    result = measure(row_dict["path"])
    return {
        "mik_pk": row_dict["mik_pk"],
        "path": row_dict["path"],
        "tier": row_dict["tier"],
        "volume": row_dict["volume"],
        "analysis_date_iso": row_dict["analysis_date_iso"],
        "reencoded_after_analysis": row_dict["reencoded_after_analysis"],
        "sample_peak_dbfs": result.get("sample_peak_dbfs"),
        "true_peak_dbtp": result.get("true_peak_dbtp"),
        "rms_db": result.get("rms_db"),
        "integrated_lufs": result.get("integrated_lufs"),
        "loudness_range_lu": result.get("loudness_range_lu"),
        "error": result.get("error"),
    }


# ------------------------------------------------------- numpy cross-check


def numpy_peak_rms(path: str) -> tuple[float, float]:
    """Recompute sample peak and RMS from raw samples, independent of astats.

    Decoded to float32 via ffmpeg so every container works, then the arithmetic
    is numpy's. ffmpeg reporting its own numbers correctly is an assumption
    worth one check rather than none.
    """
    proc = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-nostats",
            "-loglevel",
            "error",
            "-i",
            path,
            "-map",
            "0:a:0",
            "-f",
            "f32le",
            "-acodec",
            "pcm_f32le",
            "-",
        ],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(f"raw decode failed for {path}: {proc.stderr[-300:]!r}")
    samples = np.frombuffer(proc.stdout, dtype="<f4")
    peak = float(np.max(np.abs(samples)))
    rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
    def to_db(x: float) -> float:
        return 20.0 * math.log10(x) if x > 0 else -math.inf

    return to_db(peak), to_db(rms)


# ------------------------------------------------------------- statistics


@dataclass(frozen=True)
class Fit:
    """How well one candidate explains ZVOLUME."""

    candidate: str
    n: int
    pearson_r: float
    fitted_offset_db: float
    mae_about_offset_db: float
    residual_sd_db: float
    residual_p95_abs_db: float
    slope: float
    raw_mae_db: float

    def as_dict(self) -> dict[str, float | int | str]:
        return asdict(self)


def fit(candidate: str, volume: np.ndarray, measured: np.ndarray) -> Fit:
    """Pearson r, best constant offset, and the error left AFTER that offset."""
    if volume.size != measured.size:
        raise ValueError("volume and measured arrays must be the same length")
    r = float(np.corrcoef(volume, measured)[0, 1])
    offset = float(np.mean(volume - measured))
    residual = volume - measured - offset
    slope = float(np.polyfit(measured, volume, 1)[0])
    return Fit(
        candidate=candidate,
        n=int(volume.size),
        pearson_r=r,
        fitted_offset_db=offset,
        mae_about_offset_db=float(np.mean(np.abs(residual))),
        residual_sd_db=float(np.std(residual, ddof=1)),
        residual_p95_abs_db=float(np.percentile(np.abs(residual), 95)),
        slope=slope,
        raw_mae_db=float(np.mean(np.abs(volume - measured))),
    )


def fits_for(rows: list[AudioLoudness]) -> list[Fit]:
    volume = np.array([r.volume for r in rows], dtype=np.float64)
    out: list[Fit] = []
    for candidate in CANDIDATES:
        values = np.array([getattr(r, candidate) for r in rows], dtype=np.float64)
        finite = np.isfinite(values) & np.isfinite(volume)
        if int(finite.sum()) < 2:
            raise ValueError(f"candidate {candidate} has under 2 finite values")
        out.append(fit(candidate, volume[finite], values[finite]))
    return sorted(out, key=lambda f: (-abs(f.pearson_r), f.mae_about_offset_db))


def mtime_before_analysis(clean: list[AudioLoudness]) -> list[AudioLoudness]:
    """Rows whose CURRENT bytes are proven to predate MIK's own analysis.

    A row with no analysis date at all cannot be proven to be the bytes MIK
    analysed -- ``reencoded_after_analysis`` reads ``False`` for it only
    because no mtime/analysis-date comparison was possible, not because the
    comparison passed. Exclude it the same way ``scripts/key_third_opinion.py``
    excludes it, or its current (possibly re-encoded) bytes bias the fits.
    """
    return [
        m for m in clean if m.analysis_date_iso is not None and not m.reencoded_after_analysis
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--limit", type=int, default=0, help="0 means every row")
    parser.add_argument(
        "--sample",
        type=int,
        default=0,
        help=(
            "seeded RANDOM subset of the resolvable rows (0 means all). A full "
            "decode plus EBU R128 pass costs minutes per track on a contended "
            "machine; a seeded random sample is honest and reproducible where "
            "quietly taking the first N rows is neither, since the resolver "
            "returns rows in MIK primary-key order"
        ),
    )
    parser.add_argument("--seed", type=int, default=20260728)
    parser.add_argument("--cross-check", type=int, default=25)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    resolved: list[ResolvedAudio] = resolve(args.data_dir)
    usable = [r for r in resolved if r.volume is not None]
    population = len(usable)
    if args.limit:
        usable = usable[: args.limit]
    if args.sample and args.sample < len(usable):
        random.Random(args.seed).shuffle(usable)
        usable = usable[: args.sample]
    print(
        f"measuring {len(usable)} of {population} MIK rows with audio on disk, "
        f"tiers {tier_counts(usable)}",
        flush=True,
    )

    payloads = [asdict(r) for r in usable]
    measured: list[AudioLoudness] = []
    done = 0
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(_measure_row, p) for p in payloads]
        for future in as_completed(futures):
            measured.append(AudioLoudness(**future.result()))
            done += 1
            if done % 50 == 0:
                print(f"  {done}/{len(futures)} measured", flush=True)

    failed = [m for m in measured if m.error]
    clean = [m for m in measured if not m.error]
    if not clean:
        raise RuntimeError("every ffmpeg measurement failed; the pipeline is broken")
    print(f"measured {len(clean)}, ffmpeg failures {len(failed)}")

    subsets: dict[str, list[AudioLoudness]] = {
        "all_resolved": clean,
        "mtime_before_analysis": mtime_before_analysis(clean),
        "bookmark_tier_only": [m for m in clean if m.tier == "bookmark"],
    }
    subset_reports: dict[str, object] = {}
    for name, subset in subsets.items():
        if len(subset) < 2:
            subset_reports[name] = {"n": len(subset), "note": "too few rows to fit"}
            continue
        subset_reports[name] = {
            "n": len(subset),
            "by_tier": tier_counts(subset),  # type: ignore[arg-type]
            "fits": [f.as_dict() for f in fits_for(subset)],
        }
    report: dict[str, object] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "ffmpeg_failures": [{"path": m.path, "error": m.error} for m in failed],
        "subsets": subset_reports,
    }

    cross: list[dict[str, float | str]] = []
    for row in clean[: args.cross_check]:
        # `clean` already excludes ffmpeg failures (m.error), so a resolved
        # row's own peak/RMS must be present; a None here is ffmpeg lying
        # about its own success, which the cross-check must not paper over.
        if row.sample_peak_dbfs is None or row.rms_db is None:
            raise RuntimeError(
                f"{row.path}: ffmpeg reported success but left peak/RMS unset"
            )
        peak_db, rms_db = numpy_peak_rms(row.path)
        cross.append(
            {
                "path": row.path,
                "ffmpeg_peak_dbfs": row.sample_peak_dbfs,
                "numpy_peak_dbfs": peak_db,
                "ffmpeg_rms_db": row.rms_db,
                "numpy_rms_db": rms_db,
            }
        )
    if cross:
        peak_delta = max(
            abs(float(c["ffmpeg_peak_dbfs"]) - float(c["numpy_peak_dbfs"]))
            for c in cross
        )
        rms_delta = max(
            abs(float(c["ffmpeg_rms_db"]) - float(c["numpy_rms_db"])) for c in cross
        )
        report["numpy_cross_check"] = {
            "files": len(cross),
            "max_peak_delta_db": peak_delta,
            "max_rms_delta_db": rms_delta,
            "rows": cross,
        }
        print(
            f"numpy cross-check on {len(cross)} files: max peak delta "
            f"{peak_delta:.4f} dB, max RMS delta {rms_delta:.4f} dB"
        )

    report["rows"] = [asdict(m) for m in measured]
    out = args.out or args.data_dir / "state" / "equivalence-loudness-probe.json"
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")

    print("\ncandidate fits against ZSONG.ZVOLUME (subset: all_resolved)")
    header = f"{'candidate':20s} {'n':>5s} {'r':>8s} {'offset':>9s} {'MAE':>7s} {'sd':>7s} {'slope':>7s}"
    print(header)
    for f in fits_for(subsets["all_resolved"]):
        print(
            f"{f.candidate:20s} {f.n:5d} {f.pearson_r:8.4f} "
            f"{f.fitted_offset_db:+9.3f} {f.mae_about_offset_db:7.3f} "
            f"{f.residual_sd_db:7.3f} {f.slope:7.3f}"
        )
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
