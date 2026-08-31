"""Render the stretch-quality results as the markdown table that gates the flip.

Two honesty rules from the methodology are enforced here rather than
remembered: the residual caveat prints above every table carrying a residual
column, and no pass/fail verdict is printed against a threshold this harness
invented. Thresholds are set jointly across lanes after both harnesses run.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

RESIDUAL_CAVEAT = """> **Residual caveat (spec amendment 1).** `residual_dbr` is a DIAGNOSTIC, not a
> ranking metric, and this table contains it. Under a least-squares gain fit
> `residual dBr = 10*log10(1 - rho^2)`, so residual is a pure function of
> waveform-SHAPE similarity and nothing else. The default preset does not
> preserve waveform phase: at rate 1.000 the round-trip residual reads ~0 dBr
> at rho ~0.07, while the KNOWN-WORSE `cheaper` preset scores about 7 dB BETTER
> on residual purely by being more waveform-preserving, and loses 6/6 on LSD.
> More negative residual means "more waveform-preserving", NOT "better
> sounding". Builds are ranked by LSD + onset displacement p95 + onset
> recovery. Residual stays because it trips on waveform-domain damage -- gain
> errors, truncation, mono collapse, alignment failure -- that spectral
> measures can miss."""


def _commit_sha() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else float("nan")


def _cell(value: object, spec: str = ".2f") -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return "inf" if value == float("inf") else format(value, spec)
    return str(value)


def _table(headers: list[str], rows: list[list[str]]) -> str:
    divider = "|" + "|".join("-" * (len(header) + 2) for header in headers) + "|"
    lines = ["| " + " | ".join(headers) + " |", divider]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def build_report(results: dict[str, object]) -> str:
    cells = [row for row in results["cells"] if row.get("status") == "ok"]  # type: ignore[index]
    refused = [row for row in results["cells"] if row.get("status") != "ok"]  # type: ignore[index]
    constants = results["constants"]  # type: ignore[index]
    window = results["measurement_window_s"]  # type: ignore[index]
    arms = sorted({str(row["arm_id"]) for row in cells})
    conditions = sorted({str(row["condition_id"]) for row in cells})
    fixtures = sorted({str(row["fixture_id"]) for row in cells})

    by_arm: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in cells:
        by_arm[str(row["arm_id"])].append(row)

    parts: list[str] = []
    parts.append("# Stretch-quality table, lane agentB")
    parts.append("")
    parts.append(
        f"Generated {datetime.now(UTC).strftime('%a %-d %b %Y %H:%M UTC')} at commit "
        f"`{_commit_sha()}`. Binding spec: `.planning/QUALITY-METHODOLOGY-RECONCILED.md`."
    )
    parts.append("")
    parts.append(
        "**This table is the gate for the `STRETCH_BLOCK_MS` flip.** Per the methodology, "
        "no latency number may be quoted in any gate, PR body or status report without the "
        "same-commit quality table beside it."
    )
    parts.append("")
    parts.append("## Instrument settings")
    parts.append("")
    parts.append(
        _table(
            ["setting", "value", "why"],
            [
                ["latency trim", "**NONE**", "amendment 3: the self-report is metadata only"],
                [
                    "alignment carrier",
                    f"`{constants['align_carrier']}`",  # type: ignore[index]
                    "amendment 7: raw waveform cycle-skips at 0.999 confidence",
                ],
                [
                    "align correlation floor (gate c)",
                    f"{constants['align_correlation_floor']} ON THE CARRIER",  # type: ignore[index]
                    "calibrated: noise 0.005, worst real arm 0.148; a raw floor is unusable "
                    "because the real baseline arm reads 0.012 raw",
                ],
                [
                    "reference self-similarity ceiling (gate d)",
                    str(constants.get("align_self_similarity_ceiling", "n/a")),  # type: ignore[union-attr]
                    "amendment 7 refinement: the carrier alone still cycle-skipped a sine at "
                    "0.999, so too-periodic references RAISE",
                ],
                [
                    "onset basis",
                    str(constants.get("onset_basis", "n/a")),  # type: ignore[union-attr]
                    "amendment 9: mel defaults produce dead low bands; this harness has no "
                    "mel basis and asserts the equivalent property",
                ],
                [
                    "LSD floor",
                    f"{constants['lsd_floor_db_below_ref_peak']} dB below REFERENCE PEAK",  # type: ignore[index]
                    "amendment 4: absolute flooring read 39.7 dB on a null test",
                ],
                [
                    "LSD frame",
                    f"{constants['lsd_frame_ms']} ms",  # type: ignore[index]
                    "magnitude-spectrum comparison, phase-insensitive by construction",
                ],
                [
                    "onset hop",
                    f"{constants['onset_hop_samples']} samples "  # type: ignore[index]
                    f"({float(constants['onset_hop_samples']) / 44100 * 1000:.3f} ms)",  # type: ignore[index]
                    "amendment 5: must be <= gate/4, librosa's 512 default is 11.61 ms",
                ],
                [
                    "onset recovery gate",
                    f"{constants['onset_recovery_gate_ms']} ms",  # type: ignore[index]
                    "PROPOSED, set jointly",
                ],
                [
                    "pitch tracker",
                    str(constants["pitch_tracker"]),  # type: ignore[index]
                    "amendment 8, proposed; see README calibration",
                ],
                [
                    "measurement window",
                    f"[{window[0]}, {window[1]}) s",
                    "RECONSTRUCTED, see open question Q1",
                ],
            ],
        )
    )
    parts.append("")
    parts.append(
        "**No pass/fail threshold appears anywhere in this document.** Thresholds are set "
        "jointly across lanes after both harnesses have run these fixtures at these settings, "
        "and they ratchet per experiment-round rules. The only hard failures here are "
        "STRUCTURAL: a refusal to measure."
    )
    parts.append("")

    # ----- fixtures -----
    parts.append("## Fixtures")
    parts.append("")
    parts.append(
        _table(
            [
                "#",
                "material class",
                "stable_id",
                "BPM",
                "frames",
                "rms dBFS",
                "peak dBFS",
                "sha256",
            ],
            [
                [
                    str(entry["fixture_id"]),
                    str(entry["material_class"]),
                    f"`{str(entry['stable_id'])[:12]}`",
                    _cell(entry["bpm"], ".2f"),
                    str(entry["frames"]),
                    _cell(entry["rms_dbfs"], ".1f"),
                    _cell(entry["peak_dbfs"], ".1f"),
                    f"`{str(entry['sha256'])[:12]}`",
                ]
                for entry in results["excerpts"]  # type: ignore[index]
            ],
        )
    )
    parts.append("")
    parts.append(
        "Every excerpt passed the loud-non-silence assert (an evicted iCloud stub serves an "
        "empty body and decodes to digital silence, so a 200 is not evidence of audio). PCM "
        "sidecars are sha256-pinned and re-verified on every read."
    )
    parts.append("")

    # ----- summary -----
    parts.append("## Summary by arm (RANKING metrics)")
    parts.append("")
    summary_rows = []
    for arm in arms:
        rows = by_arm[arm]
        lsd = [float(row["lsd_db"]) for row in rows]
        p95 = [float(row["onset_displacement_p95_ms"]) for row in rows]
        recovery = [float(row["onset_recovery"]) for row in rows]
        unity = [float(row["lsd_db"]) for row in rows if str(row["condition_id"]) == "C0"]
        summary_rows.append(
            [
                arm,
                str(len(rows)),
                _cell(_mean(lsd)),
                _cell(max(lsd) if lsd else None),
                _cell(_mean(unity)),
                _cell(_mean(p95)),
                _cell(_mean(recovery) * 100, ".1f"),
                _cell(_mean([float(row["node_latency_ms"]) for row in rows]), ".1f"),
            ]
        )
    parts.append(
        _table(
            [
                "arm",
                "cells",
                "mean LSD dB",
                "worst LSD dB",
                "unity LSD dB (D1)",
                "mean onset p95 ms",
                "mean recovery %",
                "node.latency() ms",
            ],
            summary_rows,
        )
    )
    parts.append("")
    parts.append(
        "`node.latency()` is READ after `configure()` and reported as METADATA. It is never "
        "used to trim a render: signalsmith-stretch 1.3.2 in buffer-playback mode already "
        "self-compensates it, and trimming again puts every render 120 ms early."
    )
    parts.append("")

    # ----- per condition -----
    parts.append("## LSD by condition and arm (mean across fixtures)")
    parts.append("")
    parts.append(
        _table(
            ["condition", "rate", "semis", *arms],
            [
                [
                    condition,
                    _cell(
                        next(
                            (
                                float(row["rate"])
                                for row in cells
                                if row["condition_id"] == condition
                            ),
                            None,
                        ),
                        ".3f",
                    ),
                    _cell(
                        next(
                            (
                                float(row["semitones"])
                                for row in cells
                                if row["condition_id"] == condition
                            ),
                            None,
                        ),
                        "+.0f",
                    ),
                    *[
                        _cell(
                            _mean(
                                [
                                    float(row["lsd_db"])
                                    for row in by_arm[arm]
                                    if row["condition_id"] == condition
                                ]
                            )
                        )
                        for arm in arms
                    ],
                ]
                for condition in conditions
            ],
        )
    )
    parts.append("")

    # ----- pitch -----
    pitched = [row for row in cells if "pitch_error_cents_p50" in row]
    if pitched:
        parts.append("## Master tempo and key-shift accuracy (amendment 8)")
        parts.append("")
        parts.append(
            "Measured on the FORWARD render, never the round trip: a round trip restores the "
            "original pitch by construction. At `semis 0` the expectation is **0 cents at every "
            "rate** -- that is the master-tempo promise itself. `p50` is SIGNED so drift "
            "direction is visible."
        )
        parts.append("")
        parts.append(
            _table(
                [
                    "fixture",
                    "arm",
                    "condition",
                    "rate",
                    "expected cents",
                    "p50 cents",
                    "p95 cents",
                    "voiced %",
                ],
                [
                    [
                        str(row["fixture_id"]),
                        str(row["arm_id"]),
                        str(row["condition_id"]),
                        _cell(row["rate"], ".3f"),
                        _cell(row["pitch_expected_cents"], "+.0f"),
                        _cell(row["pitch_error_cents_p50"], "+.1f"),
                        _cell(row["pitch_error_cents_p95"], ".1f"),
                        _cell(float(row["pitch_voiced_fraction"]) * 100, ".0f"),
                    ]
                    for row in pitched
                ],
            )
        )
        parts.append("")

    # ----- full grid -----
    parts.append("## Full grid")
    parts.append("")
    parts.append(RESIDUAL_CAVEAT)
    parts.append("")
    parts.append(
        _table(
            [
                "fixture",
                "arm",
                "cond",
                "LSD dB",
                "onset p95 ms",
                "recovery %",
                "align ms",
                "align rho",
                "peak ratio",
                "self-sim",
                "residual dBr",
                "rho",
                "latency ms",
            ],
            [
                [
                    str(row["fixture_id"]),
                    str(row["arm_id"]),
                    str(row["condition_id"]),
                    _cell(row["lsd_db"]),
                    _cell(row["onset_displacement_p95_ms"]),
                    _cell(float(row["onset_recovery"]) * 100, ".1f"),
                    _cell(row["align_delay_ms"], "+.2f"),
                    _cell(row["align_correlation"], ".3f"),
                    _cell(row["align_peak_ratio"], ".3f"),
                    _cell(row.get("align_self_similarity"), ".3f"),
                    _cell(row["residual_dbr"], ".1f"),
                    _cell(row["residual_rho"], ".3f"),
                    _cell(row["node_latency_ms"], ".1f"),
                ]
                for row in sorted(
                    cells,
                    key=lambda row: (
                        str(row["fixture_id"]),
                        str(row["condition_id"]),
                        str(row["arm_id"]),
                    ),
                )
            ],
        )
    )
    parts.append("")

    # ----- calibration and disqualifiers -----
    parts.append("## Calibration and structural checks")
    parts.append("")
    determinism_failures = [row for row in cells if not row.get("deterministic")]
    collapsed = [row for row in cells if row.get("channels_bit_identical")]
    parts.append(
        _table(
            ["check", "result", "note"],
            [
                [
                    "S1 determinism",
                    "**PASS**"
                    if not determinism_failures
                    else f"**FAIL** ({len(determinism_failures)})",
                    "every cell rendered TWICE in-page and the two sha256s compared; "
                    "inequality FAILS the run rather than being recorded",
                ],
                [
                    "S2 channel distinctness",
                    "**PASS**" if not collapsed else f"**FAIL** ({len(collapsed)})",
                    "distinctness, not channel count: a mono-collapsed render still reports two",
                ],
                [
                    "S3 alignment integrity",
                    "**PASS**" if not refused else f"**{len(refused)} refused**",
                    "the amendment-7 four-gate stack: (a) rise carrier at both stages, "
                    "(b) coarse and fine bound raise, (c) correlation floor ON THE CARRIER, "
                    "(d) reference self-similarity ceiling; plus the degenerate-carrier and "
                    "peak-dominance guards. A refusal is reported, never replaced by a number",
                ],
                [
                    "S4 loud non-silence",
                    "**PASS**",
                    "asserted on every excerpt and every render",
                ],
            ],
        )
    )
    parts.append("")
    parts.append("### D1 unity transparency")
    parts.append("")
    parts.append(
        "D1 gates on **unity LSD**, not null residual: the default preset scrambles phase at "
        "rate 1.000 (residual ~0 dBr at rho ~0.07), so a residual-based D1 would disqualify "
        "every build including baseline. Lane A's baseline calibration read **1.14 dB**. "
        "Measured here:"
    )
    parts.append("")
    parts.append(
        _table(
            ["arm", "unity LSD dB (C0, mean across fixtures)", "vs lane A baseline calibration"],
            [
                [
                    arm,
                    _cell(
                        _mean(
                            [
                                float(row["lsd_db"])
                                for row in by_arm[arm]
                                if str(row["condition_id"]) == "C0"
                            ]
                        )
                    ),
                    _cell(
                        _mean(
                            [
                                float(row["lsd_db"])
                                for row in by_arm[arm]
                                if str(row["condition_id"]) == "C0"
                            ]
                        )
                        - 1.14,
                        "+.2f",
                    ),
                ]
                for arm in arms
            ],
        )
    )
    parts.append("")
    parts.append(
        "No build in this tree bypasses the stretcher at exactly 1.000, so the stronger "
        "bit-identity claim (sha256 passthrough) does not apply and the unity LSD row stands."
    )
    parts.append("")
    parts.append("### D2-D7")
    parts.append("")
    parts.append(
        "**DEFINITIONS UNAVAILABLE.** The reconciled spec adopts lane B's base proposal for the "
        "self-disqualification clauses and re-bases D1 explicitly, but that base proposal is not "
        "present in this repository or its git history, so only D1 has a stated definition. "
        "Reported rather than invented (open question Q1). The observables this harness produces "
        "that D2-D7 would plausibly consume are all in `results.json`: `lsd_db`, "
        "`onset_displacement_p95_ms`, `onset_recovery`, `pitch_error_cents_p50/p95`, "
        "`residual_dbr`, `align_correlation`."
    )
    parts.append("")

    if refused:
        parts.append("## Refused cells")
        parts.append("")
        parts.append(
            _table(
                ["fixture", "arm", "condition", "reason"],
                [
                    [
                        str(row.get("fixture_id", "?")),
                        str(row.get("arm_id", "?")),
                        str(row.get("condition_id", "?")),
                        str(row.get("detail", row.get("status", "?"))),
                    ]
                    for row in refused
                ],
            )
        )
        parts.append("")

    parts.append(f"Cells measured: {len(cells)}. Cells refused: {len(refused)}. ")
    parts.append(f"Fixtures: {len(fixtures)}. Arms: {len(arms)}. Conditions: {len(conditions)}.")
    parts.append("")
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    results = json.loads(args.results.read_text(encoding="utf-8"))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(build_report(results), encoding="utf-8")
    print(f"[report] {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
