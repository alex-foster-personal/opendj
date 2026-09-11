#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["librosa>=0.10", "numpy>=1.26", "soundfile>=0.12"]
# ///
"""A THIRD, independent key opinion on the SUSPECT rekordbox-vs-MIK clusters.

Residual disagreements can cluster by delta signature. Two sources cannot
break their own tie, and counts alone cannot say which side errs. This adds
a third vote that neither source can influence: a local chroma key estimate.

**What this is NOT.** A CQT-chroma plus Krumhansl-Schmuckler template match is a
textbook baseline, not state of the art, and it is WEAKER than either commercial
detector. It cannot promote anything to PASSED. Its only legitimate uses are:

* if a cluster's third opinion lands overwhelmingly on one side, that is
  evidence about which source errs IN THAT MODE;
* if it splits, the cluster stays SUSPECT and requires independent listening.

To make either reading defensible the estimator is CALIBRATED first, on a
control sample of pairs where rekordbox and MIK AGREE. That control is the whole
argument: a vote whose accuracy against a known consensus is unmeasured is not
evidence at any tally. Read the control number before the cluster tallies.

Run::

    scripts/key_third_opinion.py --data-dir /path/to/data --control 120
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import subprocess
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mik_audio_resolve import ResolvedAudio, resolve

# Krumhansl-Kessler probe-tone profiles (Krumhansl 1990), the standard baseline.
KK_MAJOR = np.array(
    [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
)
KK_MINOR = np.array(
    [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
)
# Temperley (2001) revised profiles, used only as a robustness cross-check: two
# templates disagreeing on a track is a sign the audio is genuinely ambiguous,
# which is worth reporting rather than hiding behind one number.
TEMPERLEY_MAJOR = np.array([5.0, 2.0, 3.5, 2.0, 4.5, 4.0, 2.0, 4.5, 2.0, 3.5, 1.5, 4.0])
TEMPERLEY_MINOR = np.array([5.0, 2.0, 3.5, 4.5, 2.0, 4.0, 2.0, 4.5, 3.5, 2.0, 1.5, 4.0])

CLUSTERS_OF_INTEREST: tuple[str, ...] = (
    "camelot_hop_+1_fifth_up",
    "parallel_major_minor",
    "relative_major_minor",
)

_CANON = re.compile(r"pc=(\d+),(maj|min)")
ANALYSIS_SECONDS = 150.0
"""Decode at most this much audio per track, from 20 s in. A key estimate does
not improve with the whole file and the intro is the least representative part."""
ANALYSIS_OFFSET_SECONDS = 20.0
SAMPLE_RATE = 22050


@dataclass(frozen=True)
class KeyEstimate:
    """One track's third opinion, plus the disagreement it was asked about."""

    mik_pk: int
    path: str
    pair_tier: str | None
    signature: str
    rb_pc: int | None
    rb_mode: str | None
    mik_pc: int | None
    mik_mode: str | None
    mik_key_confidence: float | None
    est_pc: int | None
    est_mode: str | None
    est_margin: float | None
    est_pc_temperley: int | None
    est_mode_temperley: str | None
    templates_agree: bool | None
    verdict: str
    """``rekordbox`` / ``mik`` / ``neither`` / ``both`` / ``error``."""
    error: str | None = None


def parse_canonical(text: str) -> tuple[int | None, str | None]:
    """``'3B (pc=1,maj)'`` -> ``(1, 'maj')``. Returns ``(None, None)`` if absent."""
    hit = _CANON.search(text or "")
    if hit is None:
        return None, None
    return int(hit.group(1)), hit.group(2)


def decode_mono(path: str) -> np.ndarray:
    """Decode a bounded window to mono float32 at 22.05 kHz, via ffmpeg.

    ffmpeg rather than ``librosa.load`` avoids relying on audioread for
    container support. Offloaded placeholders must fail loudly rather than
    yield silence.
    """
    proc = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-nostats",
            "-loglevel",
            "error",
            "-ss",
            str(ANALYSIS_OFFSET_SECONDS),
            "-t",
            str(ANALYSIS_SECONDS),
            "-i",
            path,
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-f",
            "f32le",
            "-acodec",
            "pcm_f32le",
            "-",
        ],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"decode failed: {proc.stderr.decode(errors='replace')[-200:]}"
        )
    samples = np.frombuffer(proc.stdout, dtype="<f4").astype(np.float64)
    if samples.size < SAMPLE_RATE * 5:
        raise RuntimeError(f"only {samples.size} samples decoded, too short to judge")
    return samples


def chroma_profile(samples: np.ndarray) -> np.ndarray:
    """Median CQT chroma over the harmonic component, normalised to sum 1."""
    import librosa

    harmonic = librosa.effects.harmonic(samples, margin=3.0)
    chroma = librosa.feature.chroma_cqt(y=harmonic, sr=SAMPLE_RATE, bins_per_octave=36)
    profile = np.median(chroma, axis=1)
    total = float(profile.sum())
    if total <= 0:
        raise RuntimeError("chroma profile is all zeros; the audio is silent")
    return profile / total


def _best_key(
    profile: np.ndarray, major: np.ndarray, minor: np.ndarray
) -> tuple[int, str, float]:
    """Krumhansl-Schmuckler: correlate against all 24 rotated templates."""
    scores: list[tuple[float, int, str]] = []
    centred = profile - profile.mean()
    denominator = float(np.sqrt(np.sum(centred**2)))
    for pc in range(12):
        for mode, template in (("maj", major), ("min", minor)):
            rotated = np.roll(template, pc)
            rotated = rotated - rotated.mean()
            scale = denominator * float(np.sqrt(np.sum(rotated**2)))
            scores.append(
                (float(np.dot(centred, rotated) / scale) if scale else 0.0, pc, mode)
            )
    scores.sort(reverse=True)
    margin = scores[0][0] - scores[1][0]
    return scores[0][1], scores[0][2], margin


def estimate(path: str) -> dict[str, object]:
    """Third opinion for one file, with both template sets."""
    profile = chroma_profile(decode_mono(path))
    pc, mode, margin = _best_key(profile, KK_MAJOR, KK_MINOR)
    t_pc, t_mode, _ = _best_key(profile, TEMPERLEY_MAJOR, TEMPERLEY_MINOR)
    return {
        "est_pc": pc,
        "est_mode": mode,
        "est_margin": margin,
        "est_pc_temperley": t_pc,
        "est_mode_temperley": t_mode,
        "templates_agree": (pc, mode) == (t_pc, t_mode),
    }


def _vote(row: dict) -> dict:
    try:
        result = estimate(row["path"])
    except (RuntimeError, OSError, ValueError) as exc:
        return {
            **row,
            "verdict": "error",
            "error": str(exc)[:300],
            "est_pc": None,
            "est_mode": None,
            "est_margin": None,
            "est_pc_temperley": None,
            "est_mode_temperley": None,
            "templates_agree": None,
        }
    rb = (row["rb_pc"], row["rb_mode"])
    mik = (row["mik_pc"], row["mik_mode"])
    est = (result["est_pc"], result["est_mode"])
    if est == rb and est == mik:
        verdict = "both"
    elif est == rb:
        verdict = "rekordbox"
    elif est == mik:
        verdict = "mik"
    else:
        verdict = "neither"
    return {**row, **result, "verdict": verdict, "error": None}


# ----------------------------------------------------------------- inputs


def usable_audio(rows: list[ResolvedAudio]) -> tuple[dict[int, dict], int, int]:
    """Rows whose CURRENT bytes are the ones MIK actually analysed.

    A row modified after MIK analysed it (``reencoded_after_analysis``) votes
    on a different byte stream than the one that produced MIK's key, so it is
    excluded outright: admitting it would let the chroma vote falsely favor
    either source when adjudicating a suspect cluster. A row with no analysis
    date at all is a SEPARATE reason for exclusion (unknown, not confirmed
    matching) and is counted separately so the two do not read as one number.
    """
    reencoded = 0
    unknown_analysis = 0
    usable: dict[int, dict] = {}
    for row in rows:
        if row.analysis_date_iso is None:
            unknown_analysis += 1
            continue
        if row.reencoded_after_analysis:
            reencoded += 1
            continue
        usable[row.mik_pk] = asdict(row)
    return usable, reencoded, unknown_analysis


def disagreement_rows(csv_path: Path, audio: dict[int, dict]) -> list[dict]:
    """Every key disagreement that has audio on disk, with its cluster."""
    if not csv_path.exists():
        raise FileNotFoundError(
            f"{csv_path} not found; run `python -m apps.equivalence run` first"
        )
    out: list[dict] = []
    with csv_path.open(encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            pk = int(row["mik_pk"])
            hit = audio.get(pk)
            if hit is None:
                continue
            rb_pc, rb_mode = parse_canonical(row["left_canonical"])
            mik_pc, mik_mode = parse_canonical(row["right_canonical"])
            confidence = row.get("mik_key_confidence") or ""
            out.append(
                {
                    "mik_pk": pk,
                    "path": hit["path"],
                    "pair_tier": row["tier"],
                    "signature": row["signature"],
                    "rb_pc": rb_pc,
                    "rb_mode": rb_mode,
                    "mik_pc": mik_pc,
                    "mik_mode": mik_mode,
                    "mik_key_confidence": float(confidence) if confidence else None,
                }
            )
    return out


def control_rows(
    data_dir: Path, audio: dict[int, dict], sample: int, seed: int
) -> list[dict]:
    """Pairs where rekordbox and MIK AGREE, to calibrate the estimator."""
    from apps.equivalence.compare import compare_pair
    from apps.equivalence.config import CFG, FIELD_PAIRS
    from apps.equivalence.sources import match, read_mik, read_rekordbox

    CFG.resolve(data_dir)
    rb_rows = read_rekordbox(CFG.rekordbox_db)
    mik_rows = read_mik(CFG.mik_db)
    pairings, _ = match(rb_rows, mik_rows)
    pair = next(p for p in FIELD_PAIRS if p.field_name == "key")
    _agreement, disagreements = compare_pair(pairings, pair)
    disagreeing = {int(d.right_id) for d in disagreements}

    from apps.equivalence.normalisers import MISSING, normalise

    candidates: list[dict] = []
    for pairing in pairings:
        pk = pairing.right.pk
        if pk in disagreeing or pk not in audio:
            continue
        left = normalise(pairing.left.key_raw, "key", pair.left.unit)
        right = normalise(pairing.right.key_camelot, "key", pair.right.unit)
        if left is MISSING or right is MISSING or left != right:
            continue
        pc, mode = int(left.pitch_class), str(left.mode)  # type: ignore[union-attr]
        candidates.append(
            {
                "mik_pk": pk,
                "path": audio[pk]["path"],
                "pair_tier": pairing.tier,
                "signature": "CONTROL_sources_agree",
                "rb_pc": pc,
                "rb_mode": mode,
                "mik_pc": pc,
                "mik_mode": mode,
                "mik_key_confidence": pairing.right.key_confidence,
            }
        )
    random.Random(seed).shuffle(candidates)
    return candidates[:sample]


# ----------------------------------------------------------------- tally


def tally(rows: list[KeyEstimate]) -> dict[str, dict[str, int]]:
    out: dict[str, Counter] = {}
    for row in rows:
        out.setdefault(row.signature, Counter())[row.verdict] += 1
    return {k: dict(sorted(v.items())) for k, v in sorted(out.items())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--control", type=int, default=120)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--seed", type=int, default=20260728)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    audio, reencoded, unknown_analysis = usable_audio(resolve(args.data_dir))
    csv_path = (
        args.data_dir / "state" / "equivalence-disagreements" / "key-disagreements.csv"
    )
    disagreements = disagreement_rows(csv_path, audio)
    controls = control_rows(args.data_dir, audio, args.control, args.seed)
    print(
        f"key disagreements with audio on disk: {len(disagreements)}; "
        f"control (sources agree) sample: {len(controls)}",
        flush=True,
    )
    print(
        f"excluded from vote: {reencoded} reencoded after analysis, "
        f"{unknown_analysis} with no analysis date",
        flush=True,
    )

    jobs = disagreements + controls
    results: list[KeyEstimate] = []
    done = 0
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(_vote, row) for row in jobs]
        for future in as_completed(futures):
            results.append(KeyEstimate(**future.result()))
            done += 1
            if done % 25 == 0:
                print(f"  {done}/{len(futures)} estimated", flush=True)

    ok = [r for r in results if r.verdict != "error"]
    errors = [r for r in results if r.verdict == "error"]
    control = [r for r in ok if r.signature == "CONTROL_sources_agree"]
    clusters = [r for r in ok if r.signature != "CONTROL_sources_agree"]

    control_hits = sum(1 for r in control if r.verdict == "both")
    payload: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "method": (
            "ffmpeg decode of a 150 s window from 20 s in, mono 22.05 kHz; "
            "librosa harmonic separation; CQT chroma at 36 bins/octave; median "
            "over frames; Krumhansl-Schmuckler correlation against 24 rotated "
            "Krumhansl-Kessler templates, with Temperley templates as a "
            "robustness cross-check. A textbook baseline, WEAKER than either "
            "commercial detector. It is a tie-break vote, never a verdict."
        ),
        "errors": [
            {"mik_pk": r.mik_pk, "path": r.path, "error": r.error} for r in errors
        ],
        "calibration_control": {
            "n": len(control),
            "agrees_with_both_sources": control_hits,
            "accuracy_vs_consensus": (control_hits / len(control) if control else None),
            "templates_agree": sum(1 for r in control if r.templates_agree),
            "meaning": (
                "share of tracks where rekordbox and MIK AGREE on which this "
                "estimator reproduces that consensus. This is the ceiling on "
                "how much any cluster tally below is worth."
            ),
        },
        "by_cluster": tally(clusters),
        "by_cluster_tier": {
            sig: dict(Counter(r.pair_tier for r in clusters if r.signature == sig))
            for sig in sorted({r.signature for r in clusters})
        },
        "rows": [asdict(r) for r in results],
    }
    out = args.out or args.data_dir / "state" / "equivalence-key-third-opinion.json"
    out.write_text(json.dumps(payload, indent=1), encoding="utf-8")

    print(
        f"\nCALIBRATION: the estimator reproduces the rekordbox+MIK consensus on "
        f"{control_hits}/{len(control)} control tracks "
        f"({control_hits / len(control):.1%})"
        if control
        else "\nCALIBRATION: no control tracks resolved"
    )
    print("\nper-cluster third-opinion tally")
    print(f"{'cluster':30s} {'n':>4s} {'rekordbox':>10s} {'mik':>5s} {'neither':>8s}")
    for sig, counts in payload["by_cluster"].items():
        total = sum(counts.values())
        print(
            f"{sig:30s} {total:4d} {counts.get('rekordbox', 0):10d} "
            f"{counts.get('mik', 0):5d} {counts.get('neither', 0):8d}"
        )
    print(f"\nestimation errors: {len(errors)}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
