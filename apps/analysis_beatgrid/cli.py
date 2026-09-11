"""Turn `beat_this_runner.py` output into the lane's analysis, and print it.

THE SECOND HALF OF THE PRODUCER, and the agent-native surface for it: the
runner needs torch and therefore its own PEP 723 environment, while everything
downstream of the beat times is pure stdlib and lives in the repo venv. This is
the seam between them, and it is a real CLI rather than a private function so
the whole pipeline can be driven and inspected without a UI (CLAUDE.md
agent-native parity).

    uv run --no-project --script apps/analysis_beatgrid/beat_this_runner.py \\
        --audio track.wav --out /tmp/beats.json --device cpu
    .venv/bin/python -m apps.analysis_beatgrid.cli --beats /tmp/beats.json

It writes nothing to the store. `AnalysisRecord` rows are the nav1-contract
lane's job in wave 1; this lane delivers the numbers and the flags that go into
one, plus the producer identity the runner already stamped so a record can
carry it unchanged.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from apps.analysis_beatgrid.bar_phase import BarPhase, assign_bar_phase
from apps.analysis_beatgrid.bpm import estimate_bpm
from apps.analysis_beatgrid.flags import evaluate_pulse
from apps.analysis_beatgrid.tempo_change import detect_tempo_changes


def _bar_phase_fields(phase: BarPhase) -> dict[str, Any]:
    """The 1..`BAR_BEATS` phase, plus every count that qualifies it.

    The anomaly counts travel WITH the numbers rather than beside them in a log,
    because a wrapped `n` alone cannot tell a consumer whether the bar it sits in
    was the length the wrap assumed. See `bar_phase.py`.
    """
    return {
        "beat_numbers": phase.beat_numbers,
        "bar_phase_unestablished": phase.bar_phase_unestablished,
        "bar_phase_reason": phase.reason,
        "n_backprojected_beats": phase.n_backprojected_beats,
        "n_bars_over_length": phase.n_bars_over_length,
        "n_bars_under_length": phase.n_bars_under_length,
        "max_bar_beats": phase.max_bar_beats,
    }


def _phase_summary(analysis: dict[str, Any]) -> str:
    """One human-readable clause for the bar phase, anomalies included."""
    if analysis["bar_phase_unestablished"]:
        return f"NO BAR PHASE ({analysis['bar_phase_reason']})"
    return (
        f"bar phase from {analysis['n_backprojected_beats']} pickup beat(s), "
        f"{analysis['n_bars_over_length']} long / "
        f"{analysis['n_bars_under_length']} short bar(s), "
        f"longest {analysis['max_bar_beats']} beats"
    )


def analyze(
    result: dict[str, Any],
    prior_bpm: float | None = None,
    *,
    threshold: float,
) -> dict[str, Any]:
    """One track's beats and activation peak into one analysis payload.

    `threshold` is REQUIRED and is the peak threshold the producer actually ran
    with, not this module's idea of one. The runner's picker keeps a frame at
    `--threshold`, so a run at the 0.35 arm accepts tracks whose peak lies
    between 0.35 and 0.5; re-judging them here against a fixed 0.5 would reject
    downstream what the producer accepted upstream and make a lower-threshold
    configuration unshippable (Codex P2 on PR #1514).

    `prior_bpm` reaches `estimate_bpm` with `scoring=True` and exists ONLY for
    benchmark octave attribution. The runtime path passes None, and `bpm.py`
    raises rather than silently ignoring a prior that arrives without the flag.
    """
    if result.get("error"):
        return {"status": "failed", "reason": result["error"], "beats": 0}

    beats = result["beats"]
    pulse = evaluate_pulse(
        beats, result.get("activation_peak"), result.get("downbeats"), threshold=threshold
    )
    if pulse.no_trackable_pulse:
        return {
            "status": "failed",
            "reason": pulse.reason,
            "beats": pulse.n_beats,
            "downbeats": pulse.n_downbeats,
            "activation_peak": pulse.activation_peak,
        }

    tempo = estimate_bpm(beats, prior_bpm, scoring=prior_bpm is not None)
    if tempo is None:
        return {
            "status": "failed",
            "reason": "no_tempo_fit",
            "beats": len(beats),
            "activation_peak": pulse.activation_peak,
        }
    changes = detect_tempo_changes(beats)
    phase = assign_bar_phase(beats, result.get("downbeats") or [])

    return {
        "status": "ok",
        "beats": len(beats),
        "downbeats": len(result.get("downbeats") or []),
        "activation_peak": pulse.activation_peak,
        **_bar_phase_fields(phase),
        "bpm": round(tempo.bpm, 2),
        "bpm_raw": round(tempo.raw_bpm, 4),
        "bpm_confidence": tempo.confidence,
        "octave_multiple": tempo.octave_multiple,
        "octave_reason": tempo.octave_reason,
        "octave_ambiguous": tempo.octave_ambiguous,
        "residual_rms_ms": round(tempo.residual_rms_s * 1000.0, 2),
        "static_grid_untrusted": changes.static_grid_untrusted,
        "tempo_changes": [
            {
                "at_s": m.at_s,
                "bpm_before": round(m.bpm_before, 2),
                "bpm_after": round(m.bpm_after, 2),
                "confidence": m.confidence,
            }
            for m in changes.markers
        ],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--beats", required=True, help="JSON written by beat_this_runner.py")
    ap.add_argument("--out", help="write the analyses here as JSON")
    ap.add_argument(
        "--prior-bpm",
        type=float,
        help="SCORING ONLY: rekordbox's stored BPM, for octave attribution",
    )
    args = ap.parse_args(argv)

    with open(args.beats, encoding="utf-8") as fh:
        payload = json.load(fh)

    # No hidden default: the flag must be judged against the threshold the run
    # was made at, and a payload that does not state one cannot be judged.
    threshold = payload.get("threshold")
    if threshold is None:
        raise SystemExit(
            f"[beatgrid-cli] {args.beats} records no producer `threshold`, so the "
            "pulse flag has nothing to judge the activation peak against; re-run "
            "beat_this_runner.py, which stamps the threshold it used"
        )

    results = payload["results"]
    # ONE prior cannot describe MANY tracks. `--prior-bpm` is a per-track
    # rekordbox figure used for octave attribution, so applying it across a
    # multi-track payload would push every ambiguous track towards one other
    # track's tempo and silently corrupt the scoring analysis (Codex P2 on
    # PR #1514). Refused rather than applied to the first row or ignored.
    if args.prior_bpm is not None and len(results) != 1:
        raise SystemExit(
            f"[beatgrid-cli] --prior-bpm describes ONE track but {args.beats} holds "
            f"{len(results)} results; run the CLI per track, or drop the prior"
        )

    analyses = {
        path: analyze(result, args.prior_bpm, threshold=float(threshold))
        for path, result in results.items()
    }

    print(
        f"[beatgrid-cli] producer {payload.get('producer')} "
        f"{payload.get('producer_version')} on {payload.get('device')}, "
        f"threshold {payload.get('threshold')}",
        flush=True,
    )
    for path, a in analyses.items():
        name = path.rsplit("/", 1)[-1]
        if a["status"] != "ok":
            print(f"[beatgrid-cli] {name}: FAILED ({a['reason']})", flush=True)
            continue
        print(
            f"[beatgrid-cli] {name}: {a['bpm']:.2f} BPM "
            f"(conf {a['bpm_confidence']:.3f}, octave {a['octave_reason']}"
            f"{', AMBIGUOUS' if a['octave_ambiguous'] else ''}), "
            f"{a['beats']} beats, {a['downbeats']} downbeats, "
            f"{_phase_summary(a)}, "
            f"residual {a['residual_rms_ms']:.1f} ms, "
            f"{len(a['tempo_changes'])} tempo change(s)"
            f"{' STATIC GRID UNTRUSTED' if a['static_grid_untrusted'] else ''}",
            flush=True,
        )
        for m in a["tempo_changes"]:
            print(
                f"[beatgrid-cli]     change at {m['at_s']:.2f}s: "
                f"{m['bpm_before']:.2f} -> {m['bpm_after']:.2f} BPM "
                f"(confidence {m['confidence']:.3f})",
                flush=True,
            )

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"source": args.beats, "analyses": analyses}, fh, indent=1)
        print(f"[beatgrid-cli] -> {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
