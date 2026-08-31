"""Score every rendered cell against its reference excerpt.

Reads the render manifest the Playwright spec wrote, re-verifies every PCM
sha256, and produces the machine-readable results the report renders.

RANKING metrics are LSD, onset displacement p95 and onset recovery (amendment
1). Residual dBr is carried as a DIAGNOSTIC only and never ranks anything.
Pitch error in cents is carried on the pitched fixtures (amendment 8) and is
measured on the FORWARD render, never the round trip.

A cell that cannot be measured is reported as a FAILURE with its reason. It is
never given a plausible-looking number.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from scripts.quality import metrics

#: RECONSTRUCTED, not quoted -- see ops/quality/stretch/README.md open question
#: Q1. The spec adopts "the measurement window" from lane B's base proposal,
#: which is not in this repo. Half a second at each end of the 10 s excerpt is
#: dropped so neither the stretcher's start ramp nor its buffer-end behaviour
#: reaches a metric.
MEASUREMENT_WINDOW_START_S = 0.5
MEASUREMENT_WINDOW_END_S = 9.5


def _window(signal: np.ndarray, sample_rate_hz: int) -> np.ndarray:
    start = int(MEASUREMENT_WINDOW_START_S * sample_rate_hz)
    end = int(MEASUREMENT_WINDOW_END_S * sample_rate_hz)
    if signal.shape[-1] < end:
        raise metrics.HarnessIntegrityError(
            f"signal of {signal.shape[-1]} samples is shorter than the measurement window end "
            f"at {end}"
        )
    return signal[..., start:end]


def score_cell(
    excerpt: np.ndarray,
    cell: dict[str, object],
    sample_rate_hz: int,
    pitched: bool,
) -> dict[str, object]:
    """Score one cell, or return why it could not be scored."""
    label = f"{cell['fixture_id']}/{cell['arm_id']}/{cell['condition_id']}"
    row: dict[str, object] = {
        "fixture_id": cell["fixture_id"],
        "arm_id": cell["arm_id"],
        "condition_id": cell["condition_id"],
        "rate": cell["rate"],
        "semitones": cell["semitones"],
        "node_latency_ms": cell["node_latency_ms"],
        "deterministic": cell["deterministic"],
        "round_trip_sha256": cell["round_trip_sha256"],
        "channels_bit_identical": cell["channels_bit_identical"],
    }

    try:
        rendered, _ = metrics.read_pcm(Path(str(cell["round_trip_path"])))
        metrics.assert_loud_non_silence(rendered, f"{label} render")
        metrics.assert_channel_distinctness_preserved(excerpt, rendered, label)

        alignment = metrics.estimate_delay_samples(excerpt, rendered, sample_rate_hz, label)
        aligned_reference, aligned_render = metrics.apply_delay(
            excerpt, rendered, alignment.delay_samples
        )
        reference_window = _window(aligned_reference, sample_rate_hz)
        render_window = _window(aligned_render, sample_rate_hz)

        residual = metrics.residual_dbr(reference_window, render_window)
        onsets = metrics.compare_onsets(reference_window, render_window, sample_rate_hz)
        row.update(
            {
                "status": "ok",
                "align_delay_samples": alignment.delay_samples,
                "align_delay_ms": alignment.delay_samples / sample_rate_hz * 1000.0,
                "align_correlation": alignment.correlation,
                "align_peak_ratio": alignment.peak_ratio,
                "align_runner_up_delay_samples": alignment.runner_up_delay_samples,
                "align_self_similarity": alignment.self_similarity,
                "align_self_similarity_delay_samples": alignment.self_similarity_delay_samples,
                "align_carrier": alignment.carrier,
                "lsd_db": metrics.log_spectral_distance(
                    reference_window, render_window, sample_rate_hz
                ),
                "residual_dbr": residual.residual_dbr,
                "residual_rho": residual.correlation_rho,
                "onset_displacement_p95_ms": onsets.displacement_p95_ms,
                "onset_displacement_median_ms": onsets.displacement_median_ms,
                "onset_recovery": onsets.recovery_fraction,
                "onset_reference_count": onsets.reference_onsets,
                "onset_recovery_gate_ms": onsets.recovery_gate_ms,
            }
        )
    except metrics.HarnessIntegrityError as error:
        row.update({"status": "REFUSED", "detail": f"{type(error).__name__}: {error}"})
        return row

    if pitched and cell.get("forward_path"):
        try:
            forward, _ = metrics.read_pcm(Path(str(cell["forward_path"])))
            pitch = metrics.pitch_error_cents(
                excerpt,
                forward,
                rate=float(cell["rate"]),  # type: ignore[arg-type]
                semitones=float(cell["semitones"]),  # type: ignore[arg-type]
                sample_rate_hz=sample_rate_hz,
            )
            row.update(
                {
                    "pitch_expected_cents": pitch.expected_cents,
                    "pitch_error_cents_p50": pitch.p50_cents,
                    "pitch_error_cents_p95": pitch.p95_cents,
                    "pitch_voiced_frames": pitch.voiced_frames,
                    "pitch_voiced_fraction": pitch.voiced_fraction,
                }
            )
        except metrics.HarnessIntegrityError as error:
            row["pitch_detail"] = f"{type(error).__name__}: {error}"
    return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    sample_rate_hz = int(manifest["sample_rate_hz"])
    excerpt_rows = {str(entry["fixture_id"]): entry for entry in manifest["excerpts"]}

    excerpts: dict[str, np.ndarray] = {}
    excerpt_report: list[dict[str, object]] = []
    for fixture_id, entry in excerpt_rows.items():
        samples, meta = metrics.read_pcm(Path(str(entry["path"])))
        rms_dbfs, peak_dbfs = metrics.assert_loud_non_silence(samples, f"{fixture_id} excerpt")
        excerpts[fixture_id] = samples
        excerpt_report.append(
            {
                "fixture_id": fixture_id,
                "material_class": entry["material_class"],
                "stable_id": entry["stable_id"],
                "bpm": entry["bpm"],
                "pitched": entry["pitched"],
                "frames": meta.frames,
                "sha256": meta.sha256,
                "rms_dbfs": rms_dbfs,
                "peak_dbfs": peak_dbfs,
                "channels_bit_identical": entry["channels_bit_identical"],
            }
        )
        print(
            f"[excerpt] {fixture_id} {meta.frames} frames rms {rms_dbfs:.1f} dBFS "
            f"peak {peak_dbfs:.1f} dBFS sha {meta.sha256[:12]}"
        )

    rows: list[dict[str, object]] = []
    for cell in manifest["cells"]:
        if cell.get("status") != "ok":
            rows.append(dict(cell))
            continue
        fixture_id = str(cell["fixture_id"])
        row = score_cell(
            excerpts[fixture_id],
            cell,
            sample_rate_hz,
            pitched=bool(excerpt_rows[fixture_id]["pitched"]),
        )
        rows.append(row)
        if row["status"] == "ok":
            print(
                f"[cell] {fixture_id}/{row['arm_id']}/{row['condition_id']} "
                f"LSD {row['lsd_db']:.2f} dB  onsetP95 {row['onset_displacement_p95_ms']:.2f} ms  "
                f"recovery {float(row['onset_recovery']):.0%}  "
                f"align {row['align_delay_ms']:+.2f} ms @ rho {row['align_correlation']:.3f}"
            )
        else:
            print(f"[REFUSED] {fixture_id}/{row['arm_id']}/{row['condition_id']}: {row['detail']}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "sample_rate_hz": sample_rate_hz,
                "measurement_window_s": [MEASUREMENT_WINDOW_START_S, MEASUREMENT_WINDOW_END_S],
                "constants": {
                    "onset_hop_samples": metrics.ONSET_HOP_SAMPLES,
                    "onset_recovery_gate_ms": metrics.ONSET_RECOVERY_GATE_MS,
                    "lsd_frame_ms": metrics.LSD_FRAME_MS,
                    "lsd_floor_db_below_ref_peak": metrics.LSD_FLOOR_DB_BELOW_REF_PEAK,
                    "align_correlation_floor": metrics.ALIGN_CORRELATION_FLOOR,
                    "align_self_similarity_ceiling": metrics.ALIGN_SELF_SIMILARITY_CEILING,
                    "align_carrier": "half-wave-rectified envelope rise",
                    "align_gates": "(a) rise carrier both stages, (b) coarse+fine bound raise, "
                    "(c) correlation floor on the carrier, (d) reference self-similarity ceiling",
                    "onset_basis": "linear-frequency STFT flux, no mel basis (amendment 9)",
                    "latency_trim": "NONE",
                    "pitch_tracker": "numpy YIN (proposed, see README)",
                },
                "excerpts": excerpt_report,
                "cells": rows,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    refused = [row for row in rows if row.get("status") != "ok"]
    print(f"[done] {len(rows)} cells, {len(refused)} refused -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
