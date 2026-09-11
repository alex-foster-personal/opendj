#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "mir_eval==0.8.2",
#     "numpy==1.26.4",
# ]
# ///
"""Score our beat tracker against HUMAN beat annotations, not against rekordbox.

WHY THIS EXISTS. Every figure in `specs/beat-mapping-bench.md` scores us
against rekordbox's PQTZ grid. Rekordbox is not ground truth: it is another
analyzer, with its own offsets and its own octave choices. An agreement figure
against it cannot say whether a disagreement is our error or theirs, which is
exactly the question a promotion decision turns on.

GTZAN-Rhythm (Marchand, Fresnel & Peeters, ISMIR 2015 LBD) is human-annotated
beat and downbeat truth over the 1000-clip GTZAN set, and it is the standard
TEST-ONLY set for beat tracking: Beat This! (Foscarin, Schlüter & Widmer,
ISMIR 2024) trains on everything else and reports **F1 0.889** here, computed
with `mir_eval`. That published number is what makes this a control rather
than just another table. Two things get measured at once:

1. OUR SCORER, against `mir_eval`, on identical beat times, for BOTH the
   F-measure and the continuity family. `mir_eval` is the reference
   implementation the literature quotes; a gap is our bug. The CONTINUITY half
   is the check `.planning/debt/1514.md` says the repo still owes: that entry
   is about CMLt and AMLt specifically, and an F-measure cross-check does not
   discharge it.
2. OUR PIPELINE, against a published figure. Reproducing ~0.889 says the
   decode, the runner and the peak picker are all sound end to end. Failing to
   reproduce it says the fault is ours and not the model's, BEFORE any
   rekordbox comparison is interpreted.

Then the question the rekordbox table cannot answer: our beats carry a
systematic median offset of about -12 ms against rekordbox (round 0 rescored,
144 of 200 fixed-tempo tracks late by more than 5 ms). Measured against HUMAN
beats, a similar offset means it is ours, and no offset means it is
rekordbox's. That is a one-line change in what we ship: a constant correction,
or none.

PROTOCOL. Standard MIREX: both reference and estimate have their first 5
seconds trimmed (`mir_eval.beat.trim_beats`), because no tracker is expected
to have locked on before then, and the published figures this compares against
were computed that way. Tolerance is +/-70 ms, matching this repo's
`BEAT_TOLERANCE_S`.

USAGE. PYTHONPATH must point at the repository root: the corpus and shard
readers, and the cross-checks against this repo's own scorer, are both
imported from it. Without it this exits on the import rather than printing
UNMEASURED figures, which have declined the control rather than passed it.

    PYTHONPATH=<repo root> uv run --script scripts/beatbench/score_gtzan.py \
        --pairs  <pairs.json: {key: [wav_path, beats_path]}> \
        --results out0.json [out1.json ...] \
        --out gtzan-scores.json
"""

from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from pathlib import Path
from typing import Any

import numpy as np

from scripts.beatbench.gates import (
    ESTABLISH_DIGEST,
    fixtures_digest_argument,
    refusals,
)
from scripts.beatbench.gtzan_corpus import (
    digest_inputs_for,
    fixtures_digest,
    load_annotation,
    load_results,
)

# mir_eval is a BENCH-ONLY dependency and is not in the repo venv, so importing
# it at module scope makes this module unimportable in the test lane. It is
# imported inside the scoring functions that actually use it instead. The
# corpus and shard readers, which need none of it, live in `gtzan_corpus` so
# they can be tested for real rather than skipped.

TRIM_S = 5.0
TOLERANCE_S = 0.07


PUBLISHED_BEAT_THIS_GTZAN_F1 = 0.889
# A second, DELIBERATELY TIGHTER tolerance for the shift sweep only. F at the
# standard +/-70 ms is by construction near-blind to a 10 ms translation, so
# it cannot arbitrate a 10 ms question: it will report "no difference" for a
# bias that is entirely real. 20 ms is the band where beat alignment starts
# to matter to a DJ (a flam between two transients), so it is the instrument
# that can actually see the effect being argued about. It is NEVER used for a
# headline figure, which stays comparable to the literature at 70 ms.
TIGHT_TOLERANCE_S = 0.02


#-----------------------------------------------------------------------------
# scoring
#-----------------------------------------------------------------------------

# Agreement bands against mir_eval, named because they mean different things.
# F_MEASURE_AGREEMENT_BAND is a REPORTING threshold: a per-clip gap above it is
# a real disagreement worth counting, not float noise, because the F-measure
# comes out of a different implementation of the same definition.
# RATIO_NOISE_FLOOR is a float-noise floor for cmlt/amlt, which are ratios of
# counted beats and so should agree exactly when both sides score the same clip;
# anything above the floor is a genuine difference in the counting.
F_MEASURE_AGREEMENT_BAND = 1e-3
RATIO_NOISE_FLOOR = 1e-6

def score_one(reference: list[float], estimate: list[float]) -> dict[str, Any] | None:
    """mir_eval scores for one clip, plus the signed offset of our beats.

    Returns None when the trimmed reference is empty, which is a clip that
    cannot be scored rather than a clip that scored zero.
    """
    import mir_eval

    ref = mir_eval.beat.trim_beats(np.array(sorted(reference), dtype=float), min_beat_time=TRIM_S)
    est = mir_eval.beat.trim_beats(np.array(sorted(estimate), dtype=float), min_beat_time=TRIM_S)
    if ref.size == 0:
        return None
    f = mir_eval.beat.f_measure(ref, est, f_measure_threshold=TOLERANCE_S)
    cmlc, cmlt, amlc, amlt = mir_eval.beat.continuity(ref, est)

    # Signed nearest-reference error per estimated beat: negative means our
    # beat lands BEFORE the human's. The median over a clip is that clip's
    # systematic offset; the median over clips is the tracker's bias.
    offsets_ms: list[float] = []
    if est.size and ref.size:
        for t in est:
            idx = int(np.argmin(np.abs(ref - t)))
            offsets_ms.append((t - ref[idx]) * 1000.0)
    return {
        "f_measure": float(f),
        "cmlc": float(cmlc), "cmlt": float(cmlt),
        "amlc": float(amlc), "amlt": float(amlt),
        "n_reference": int(ref.size), "n_estimate": int(est.size),
        "offset_median_ms": float(st.median(offsets_ms)) if offsets_ms else None,
        "offset_abs_p95_ms": (
            float(np.percentile(np.abs(offsets_ms), 95)) if offsets_ms else None
        ),
    }


def our_scorer_figures(
    reference: list[float], estimate: list[float]
) -> dict[str, float | None] | None:
    """This repo's OWN F-measure and continuity on the SAME trimmed inputs.

    Two separate cross-checks travel together here because they share the
    inputs, and they must not be conflated when the result is written up:

    - `f_measure`, from `score_positions`. Greedy nearest-neighbour pairing
      against `mir_eval`'s optimal one-to-one matching.
    - `cmlt` and `amlt`, from `score_continuity`. THIS is the one
      `.planning/debt/1514.md` records as owed. That entry found this repo's
      CMLt exactly one continuously-correct beat short of `mir_eval`'s on 4
      librosa tracks and 2 lane tracks, and could not say whether the boundary
      at fault is the candidate interval at `nearest == 0`, the strict versus
      inclusive tolerance comparison, or the tie rule in `_nearest_index`.
      998 clips of real audio is a far better denominator for that than 6.

    Imported lazily and tolerantly: this script runs in its own PEP 723
    environment, so the repo package may not be importable, and a missing
    control must report itself rather than silently skip.
    """
    import mir_eval

    try:
        from apps.analysis_bench.scorers.beatgrid import score_continuity, score_positions
    except Exception:
        return None
    ref = mir_eval.beat.trim_beats(np.array(sorted(reference), dtype=float), min_beat_time=TRIM_S)
    est = mir_eval.beat.trim_beats(np.array(sorted(estimate), dtype=float), min_beat_time=TRIM_S)
    if ref.size == 0:
        return None
    positions = score_positions(list(ref), list(est), tolerance_s=TOLERANCE_S)
    cont = score_continuity(list(ref), list(est))
    return {
        "f_measure": float(positions.f_measure),
        # None, not 0.0, when there were too few beats to score: an
        # unmeasurable case must not render as a failing one, and it must not
        # be averaged into a mean either.
        "cmlt": None if cont.cmlt is None else float(cont.cmlt),
        "amlt": None if cont.amlt is None else float(cont.amlt),
    }



def sweep_constant_shift(
    per_clip_inputs: list[tuple[list[float], list[float]]],
    shifts_ms: list[float],
) -> list[dict[str, float]]:
    """Mean mir_eval F after adding each constant shift to OUR beat times.

    WHY THIS IS THE DECIDING CONTROL, not a nice-to-have. Measuring a median
    offset of -8 ms against human beats says our beats sit early; it does NOT
    say that correcting it buys anything. The offset is a tenth of the +/-70 ms
    matching tolerance, so a bias that real can still be worth exactly zero F,
    and shipping a constant that buys zero is shipping a constant we then have
    to maintain and explain.

    So the claim "we should correct by +X ms" has to predict something
    checkable: that F RISES at +X and FALLS either side of it. This walks the
    range and reports where the peak actually lands. A peak at 0 refutes the
    correction outright. A peak at +8 that beats 0 by a hair refutes it just as
    firmly on cost grounds, and the size of the gain is the number that
    decides, not its sign.
    """
    import mir_eval

    out: list[dict[str, float]] = []
    for shift_ms in shifts_ms:
        dt = shift_ms / 1000.0
        scores: list[float] = []
        tight: list[float] = []
        for reference, estimate in per_clip_inputs:
            ref = mir_eval.beat.trim_beats(
                np.array(sorted(reference), dtype=float), min_beat_time=TRIM_S)
            est = mir_eval.beat.trim_beats(
                np.array(sorted(t + dt for t in estimate), dtype=float), min_beat_time=TRIM_S)
            if ref.size == 0:
                continue
            scores.append(float(
                mir_eval.beat.f_measure(ref, est, f_measure_threshold=TOLERANCE_S)))
            tight.append(float(
                mir_eval.beat.f_measure(ref, est, f_measure_threshold=TIGHT_TOLERANCE_S)))
        out.append({
            "shift_ms": shift_ms,
            "f_measure_mean": st.mean(scores),
            "f_measure_tight_mean": st.mean(tight),
            "n": len(scores),
        })
    return out


def score_all(
    pairs: dict[str, Any],
    results: dict[str, dict[str, Any]],
    audio_root: str,
    digest_inputs: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, list[str]], list[tuple[list[float], list[float]]]]:
    """`(per-clip scores, unscorable clips by reason, sweep inputs)`.

    `digest_inputs` comes from `gtzan_corpus.digest_inputs_for`, which covers
    every REQUESTED pair rather than every scored one, and is passed in rather
    than recomputed so the annotation digest a clip reports is byte-identical
    to the one the fixture gate hashed.

    THE EMPTY-ESTIMATE RULE, which used to be wrong here. Three states get
    confused if they are not separated deliberately:

    - **No result at all**, or a result the runner marked `error`. The analyzer
      did not answer, so there is nothing to score, and scoring it zero would
      turn a decode failure into an accuracy figure.
    - **A successful result whose beat list is EMPTY.** The analyzer DID
      answer: it looked and found no pulse, or the threshold rejected every
      peak. That is an F of exactly 0 and it belongs in the denominator. This
      code previously dropped it alongside the failures, which meant a
      candidate could raise its own benchmark score by abstaining on the clips
      it was worst at (Codex P1 BLOCKING, PR #1660).
    - **An empty trimmed reference.** There is no ground truth after the 5 s
      MIREX trim, so the clip is unscorable no matter what the analyzer said.

    The three are counted separately in the artifact, so the denominator can be
    read rather than inferred.
    """
    per_clip: dict[str, Any] = {}
    unscorable: dict[str, list[str]] = {"no_result": [], "runner_error": [], "no_reference": []}
    sweep_inputs: list[tuple[list[float], list[float]]] = []
    for key, (wav, beats_path) in sorted(pairs.items()):
        full = str(Path(audio_root) / wav) if audio_root else wav
        annotation = Path(audio_root) / beats_path if audio_root else Path(beats_path)
        r = results.get(full) or results.get(wav)
        if r is None:
            unscorable["no_result"].append(key)
            continue
        if r.get("error"):
            unscorable["runner_error"].append(key)
            continue
        beats = r.get("beats") or []
        reference, ref_downbeats = load_annotation(annotation)
        scored = score_one(reference, beats)
        if scored is None:
            unscorable["no_reference"].append(key)
            continue
        scored["genre"] = key.split("_")[0]
        our_figures = our_scorer_figures(reference, beats)
        scored["our_f_measure"] = None if our_figures is None else our_figures["f_measure"]
        scored["our_cmlt"] = None if our_figures is None else our_figures["cmlt"]
        scored["our_amlt"] = None if our_figures is None else our_figures["amlt"]
        scored["n_reference_downbeats"] = len(ref_downbeats)
        # Fixture identity, both sides, so the artifact is bound to the exact
        # bytes it was computed from rather than to their filenames.
        scored["annotation_sha256"] = digest_inputs[key]["annotation_sha256"]
        scored["decode_fingerprint"] = r.get("decode_fingerprint")
        scored["estimate_was_empty"] = not beats
        per_clip[key] = scored
        sweep_inputs.append((reference, beats))
    return per_clip, unscorable, sweep_inputs


#-----------------------------------------------------------------------------
# main
#-----------------------------------------------------------------------------

def _percentiles(xs: list[float]) -> dict[str, float]:
    s = sorted(xs)
    return {
        "p05": s[int(0.05 * (len(s) - 1))],
        "p25": s[int(0.25 * (len(s) - 1))],
        "p50": s[int(0.50 * (len(s) - 1))],
        "p75": s[int(0.75 * (len(s) - 1))],
        "p95": s[int(0.95 * (len(s) - 1))],
    }


def _report(
    args: argparse.Namespace,
    summary: dict[str, Any],
    per_clip: dict[str, Any],
    unscorable: dict[str, list[str]],
    digest: str,
) -> int:
    """Print the figures, then decide whether this run may be called a pass.

    Split out of `main` so the refusals below sit beside the numbers they
    contradict rather than at the end of a function long enough to hide them.
    """
    print(
        f"[gtzan] scored {len(per_clip)} clips "
        f"({summary['n_scored_with_empty_estimate']} of them a successful EMPTY "
        f"estimate scoring 0); unscorable {summary['unscorable_by_reason']}"
    )
    print(f"[gtzan] fixtures digest {digest}")
    print(f"[gtzan] mir_eval F={summary['f_measure_mean']:.4f} "
          f"(published {PUBLISHED_BEAT_THIS_GTZAN_F1}, "
          f"delta {summary['delta_vs_published']:+.4f})")
    print(f"[gtzan] CMLt={summary['cmlt_mean']:.4f} AMLt={summary['amlt_mean']:.4f}")
    if summary.get("our_scorer_f_mean") is not None:
        print(f"[gtzan] our scorer F={summary['our_scorer_f_mean']:.4f}, "
              f"max |gap| vs mir_eval={summary['our_scorer_max_abs_gap_vs_mir_eval']:.2e}, "
              f"clips disagreeing >1e-3: "
              f"{summary['our_scorer_clips_disagreeing_over_1e_3']}")
        for name in ("cmlt", "amlt"):
            if summary.get(f"our_{name}_n_comparable"):
                print(f"[gtzan] our {name.upper()} mean={summary[f'our_{name}_mean']:.4f} vs "
                      f"mir_eval {summary[f'mir_eval_{name}_mean_same_clips']:.4f} on "
                      f"{summary[f'our_{name}_n_comparable']} comparable clips "
                      f"({summary[f'our_{name}_n_declined']} declined); "
                      f"max |gap|={summary[f'our_{name}_max_abs_gap_vs_mir_eval']:.2e}, "
                      f"disagree>1e-6: {summary[f'our_{name}_clips_disagreeing_over_1e_6']} "
                      f"(lower {summary[f'our_{name}_clips_lower_than_mir_eval']}, "
                      f"higher {summary[f'our_{name}_clips_higher_than_mir_eval']})")
            else:
                print(f"[gtzan] our {name.upper()}: {summary.get(f'our_{name}_note')}")
    else:
        print(f"[gtzan] our scorer: {summary['our_scorer_note']}")
    if args.shift_sweep:
        print(f"[gtzan] constant-shift sweep: peak F at "
              f"{summary['shift_sweep_best_ms']:+.0f} ms "
              f"(F={summary['shift_sweep_best_f']:.4f}, "
              f"gain over 0 ms {summary['shift_sweep_gain_over_zero']:+.4f})")
        print(f"[gtzan] same sweep at +/-{int(TIGHT_TOLERANCE_S * 1000)} ms: peak F at "
              f"{summary['shift_sweep_tight_best_ms']:+.0f} ms "
              f"(F={summary['shift_sweep_tight_best_f']:.4f}, "
              f"gain over 0 ms {summary['shift_sweep_tight_gain_over_zero']:+.4f})")
    median_offset = summary["offset_vs_human_median_ms"]
    # None when every scored clip had an empty estimate, which is a legitimate
    # input (an all-abstaining candidate scores F=0 on every clip) and used to
    # raise TypeError from `+.1f` AFTER the artifact was written, so the run
    # looked like a crash rather than a result (Codex P2, PR #1660,
    # discussion_r3975279154).
    offset_text = "UNMEASURED (no clip produced a beat to offset)" if (
        median_offset is None) else f"{median_offset:+.1f} ms"
    print(f"[gtzan] offset vs HUMAN beats: median {offset_text}")
    print(f"[gtzan] -> {args.out}")

    problems = refusals(summary, unscorable)
    if problems:
        for problem in problems:
            print(f"[gtzan] FAIL {problem}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    import mir_eval

    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", required=True, help="JSON {key: [wav, beats]}")
    ap.add_argument("--results", nargs="+", required=True, help="runner output JSON(s)")
    ap.add_argument("--audio-root", default="", help="prefix joined to the wav paths in --pairs")
    ap.add_argument("--out", required=True)
    ap.add_argument("--shift-sweep", action="store_true",
                    help="also sweep a constant shift over our beats and report the F peak")
    ap.add_argument("--expect-fixtures-digest", required=True,
                    type=fixtures_digest_argument,
                    help=f"the digest the scored fixtures must hash to, or "
                         f"{ESTABLISH_DIGEST!r} to mint one for a first round")
    args = ap.parse_args()

    pairs = json.loads(Path(args.pairs).read_text())
    results, provenance = load_results([Path(p) for p in args.results])

    # Built BEFORE scoring and over every requested pair, because the fixture
    # gate asks what corpus this is, not how well the analyzer did on it.
    digest_inputs = digest_inputs_for(pairs, results, args.audio_root)
    per_clip, unscorable, sweep_inputs = score_all(
        pairs, results, args.audio_root, digest_inputs
    )

    if not per_clip:
        print("[gtzan] nothing scored: no clip had both a result and a reference", file=sys.stderr)
        return 1

    digest = fixtures_digest(digest_inputs)
    if args.expect_fixtures_digest == ESTABLISH_DIGEST:
        print(
            f"[gtzan] establishing fixtures digest {digest}. Pass it as "
            "--expect-fixtures-digest on every later round over this corpus.",
        )
    elif args.expect_fixtures_digest != digest:
        print(
            f"[gtzan] FIXTURE MISMATCH: scored fixtures hash to {digest}, "
            f"expected {args.expect_fixtures_digest}. The pair MANIFEST, the "
            "annotation bytes or the audio are not the ones this digest was "
            "recorded against, so this run is not comparable with the round it "
            "claims to extend. The manifest is named first because a truncated "
            "one is the cause this gate could not see until PR #1660: it now "
            "covers every requested pair, so a missing pair moves the digest "
            "even when every clip that remains scores identically.",
            file=sys.stderr,
        )
        return 3

    f = [c["f_measure"] for c in per_clip.values()]
    cmlt = [c["cmlt"] for c in per_clip.values()]
    amlt = [c["amlt"] for c in per_clip.values()]
    offsets = [c["offset_median_ms"] for c in per_clip.values()
               if c["offset_median_ms"] is not None]
    ours = [c["our_f_measure"] for c in per_clip.values() if c["our_f_measure"] is not None]

    summary: dict[str, Any] = {
        "n_scored": len(per_clip),
        "n_unscored": sum(len(v) for v in unscorable.values()),
        # Named separately because they mean different things: a runner error
        # is an absent measurement, while an empty estimate is a measurement of
        # zero and is INSIDE n_scored.
        "unscorable_by_reason": {k: len(v) for k, v in unscorable.items()},
        "n_scored_with_empty_estimate": sum(
            1 for c in per_clip.values() if c.get("estimate_was_empty")),
        "fixtures_digest": digest,
        "analyzer_provenance": provenance,
        "trim_s": TRIM_S,
        "tolerance_s": TOLERANCE_S,
        "mir_eval_version": getattr(mir_eval, "__version__", "unknown"),
        "f_measure_mean": st.mean(f),
        "f_measure_percentiles": _percentiles(f),
        "cmlt_mean": st.mean(cmlt),
        "amlt_mean": st.mean(amlt),
        "published_beat_this_gtzan_f1": PUBLISHED_BEAT_THIS_GTZAN_F1,
        "delta_vs_published": st.mean(f) - PUBLISHED_BEAT_THIS_GTZAN_F1,
        "offset_vs_human_median_ms": st.median(offsets) if offsets else None,
        "offset_vs_human_mean_ms": st.mean(offsets) if offsets else None,
        "offset_vs_human_percentiles_ms": _percentiles(offsets) if offsets else None,
    }
    if ours:
        summary["our_scorer_f_mean"] = st.mean(ours)
        summary["our_scorer_n"] = len(ours)
        gaps = [abs(c["our_f_measure"] - c["f_measure"]) for c in per_clip.values()
                if c["our_f_measure"] is not None]
        summary["our_scorer_max_abs_gap_vs_mir_eval"] = max(gaps)
        summary["our_scorer_clips_disagreeing_over_1e_3"] = sum(
            1 for g in gaps if g > F_MEASURE_AGREEMENT_BAND)

        # The debt-1514 control, reported SEPARATELY from the F-measure one
        # above because they are different metrics with different
        # implementations, and a pass on one says nothing about the other.
        # Denominator is clips where OUR side produced a number, so a clip we
        # declined to score cannot be counted as agreement.
        for name in ("cmlt", "amlt"):
            paired = [(c[f"our_{name}"], c[name]) for c in per_clip.values()
                      if c.get(f"our_{name}") is not None]
            summary[f"our_{name}_n_comparable"] = len(paired)
            summary[f"our_{name}_n_declined"] = len(per_clip) - len(paired)
            if not paired:
                summary[f"our_{name}_note"] = (
                    "UNMEASURED: no clip produced a comparable value on both sides.")
                continue
            deltas = [mine - theirs for mine, theirs in paired]
            summary[f"our_{name}_mean"] = st.mean(m for m, _ in paired)
            summary[f"mir_eval_{name}_mean_same_clips"] = st.mean(t for _, t in paired)
            summary[f"our_{name}_max_abs_gap_vs_mir_eval"] = max(abs(d) for d in deltas)
            summary[f"our_{name}_clips_disagreeing_over_1e_6"] = sum(
                1 for d in deltas if abs(d) > RATIO_NOISE_FLOOR)
            summary[f"our_{name}_clips_lower_than_mir_eval"] = sum(
                1 for d in deltas if d < -RATIO_NOISE_FLOOR)
            summary[f"our_{name}_clips_higher_than_mir_eval"] = sum(
                1 for d in deltas if d > RATIO_NOISE_FLOOR)
    else:
        summary["our_scorer_f_mean"] = None
        summary["our_scorer_note"] = (
            "UNMEASURED: apps.analysis_bench was not importable in this "
            "environment, so the scorer cross-check did not run. This is a "
            "capability report, not a pass."
        )

    if args.shift_sweep:
        grid = [float(x) for x in range(-30, 31, 2)]
        sweep = sweep_constant_shift(sweep_inputs, grid)
        best = max(sweep, key=lambda row: row["f_measure_mean"])
        best_tight = max(sweep, key=lambda row: row["f_measure_tight_mean"])
        at_zero = next(row for row in sweep if row["shift_ms"] == 0.0)
        summary["shift_sweep_tolerance_s"] = TOLERANCE_S
        summary["shift_sweep_tight_tolerance_s"] = TIGHT_TOLERANCE_S
        summary["shift_sweep"] = sweep
        summary["shift_sweep_best_ms"] = best["shift_ms"]
        summary["shift_sweep_best_f"] = best["f_measure_mean"]
        summary["shift_sweep_gain_over_zero"] = best["f_measure_mean"] - at_zero["f_measure_mean"]
        summary["shift_sweep_tight_best_ms"] = best_tight["shift_ms"]
        summary["shift_sweep_tight_best_f"] = best_tight["f_measure_tight_mean"]
        summary["shift_sweep_tight_gain_over_zero"] = (
            best_tight["f_measure_tight_mean"] - at_zero["f_measure_tight_mean"])

    # Per-genre, because GTZAN is 10 genres and a DJ library is not.
    by_genre: dict[str, list[float]] = {}
    for c in per_clip.values():
        by_genre.setdefault(c["genre"], []).append(c["f_measure"])
    summary["f_measure_by_genre"] = {
        g: {"n": len(v), "mean": st.mean(v)} for g, v in sorted(by_genre.items())
    }

    Path(args.out).write_text(json.dumps(
        {"schema": 2, "summary": summary, "clips": per_clip, "unscorable": unscorable},
        indent=1))

    return _report(args, summary, per_clip, unscorable, digest)


if __name__ == "__main__":
    raise SystemExit(main())
