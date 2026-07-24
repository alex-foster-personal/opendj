# /// script
# requires-python = ">=3.10"
# dependencies = ["soundfile>=0.12", "numpy<2", "scipy>=1.10"]
# ///
"""R1b: content-matched control for the instrumental-is-a-residual finding, plus a
tie-corrected human/metric agreement figure.

r1_inst_and_metric.py showed lossless ladder instrumentals cancel at ~140 dB (exact
subtraction) but the rated m4a arms cancel at 27-28 dB against a 38 dB mismatched-content
AAC control. That 10 dB gap is either (a) the m4a arms are NOT subtractions, or (b) the
control used different audio content and therefore a different codec noise level.

This settles it by building the control from EACH ARM'S OWN decoded audio, so content is
matched arm by arm. If an arm's own exact-subtraction control lands at the arm's measured
cancellation, the arm is a subtraction and its instrumental carries no independent
information. If the arm sits well below its own control, the instrumental is a real
independent stem sum.

Also recomputes human/SI-SDR agreement with the failed arm excluded, because the rater
said in writing that a failed run does not belong in the calibration set.

Requirements (mini-PRD):
  ✔︎ ✅ every arm gets a control built from its own decoded mixture and vocal, encoded at
    that arm's own measured bitrate, with the same number of codec generations.
    [if] ffprobe cannot read a bitrate [then ⛔️] RuntimeError naming the file
  ✔︎ ✅ agreement reported twice, with and without the failed arm, n stated both times.

Run:
  uv run scripts/bench/r1-offline/r1b_control.py

-Claude
"""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

REPO = Path(__file__).resolve().parents[3]
BENCH = REPO / "scripts" / "bench"
OUT = Path(__file__).resolve().parent / "r1b.json"

ARMS = [
    "hdemucs_mmi-ov0",
    "hdemucs_mmi-ov25",
    "htdemucs-ov25",
    "rb7-stems",
    "rb6-spleeter",
    "rb6-spleeter-mono-22k",
    "htdemucs-ov0-22k",
    "floor-midside",
]
# The rater's own instruction: a run that failed outright is not calibration material.
FAILED_ARM = "floor-midside"


def _decode_mono(path: Path) -> tuple[np.ndarray, int]:
    if not path.is_file():
        raise RuntimeError(f"missing: {path}")
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav = Path(tmp.name)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(path), "-c:a", "pcm_f32le", str(wav)], check=True
    )
    try:
        data, sr = sf.read(str(wav), dtype="float64", always_2d=True)
        return data.mean(axis=1), int(sr)
    finally:
        wav.unlink(missing_ok=True)


def _bitrate(path: Path) -> str:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=bit_rate", "-of", "default=nw=1:nk=1", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if not out.isdigit():
        raise RuntimeError(f"ffprobe gave no bitrate for {path}")
    return f"{int(out) // 1000}k"


def _align(*sigs: np.ndarray) -> list[np.ndarray]:
    n = min(len(s) for s in sigs)
    return [s[:n] for s in sigs]


def cancellation_db(mix: np.ndarray, voc: np.ndarray, ins: np.ndarray) -> float:
    m, v, i = _align(mix, voc, ins)
    r = m - (v + i)
    return float(10.0 * np.log10(np.dot(m, m) / max(np.dot(r, r), 1e-30)))


def _roundtrip(sig: np.ndarray, sr: int, bitrate: str, td: Path, name: str) -> np.ndarray:
    wav, m4a = td / f"{name}.wav", td / f"{name}.m4a"
    sf.write(str(wav), sig, sr, subtype="FLOAT")
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(wav), "-c:a", "aac", "-b:a", bitrate, str(m4a)],
        check=True,
    )
    return _decode_mono(m4a)[0]


def spearman(a: list[float], b: list[float]) -> float:
    ra, rb = np.asarray(_rank(a)), np.asarray(_rank(b))
    ra, rb = ra - ra.mean(), rb - rb.mean()
    return float(np.dot(ra, rb) / np.sqrt(np.dot(ra, ra) * np.dot(rb, rb)))


def _rank(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return ranks


def main() -> None:
    mix, mix_sr = _decode_mono(BENCH / "clips-cheap/zeno-signs-mixture.m4a")
    rows: list[dict] = []
    for arm in ARMS:
        voc_path = BENCH / f"clips-cheap/zeno-signs-{arm}.m4a"
        ins_path = BENCH / f"clips-cheap/zeno-signs-{arm}.inst.m4a"
        voc, voc_sr = _decode_mono(voc_path)
        ins, _ = _decode_mono(ins_path)
        if voc_sr != mix_sr:
            raise RuntimeError(f"sample-rate mismatch on {arm}")
        measured = cancellation_db(mix, voc, ins)

        # Control: this arm's own vocal, with an EXACT residual instrumental, pushed through
        # the same codec generations at the arm's own bitrates.
        m, v = _align(mix, voc)
        exact_inst = m - v
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            v_rt = _roundtrip(v, mix_sr, _bitrate(voc_path), tmp, "voc")
            i_rt = _roundtrip(exact_inst, mix_sr, _bitrate(ins_path), tmp, "ins")
            m_rt = _roundtrip(m, mix_sr, _bitrate(BENCH / "clips-cheap/zeno-signs-mixture.m4a"), tmp, "mix")
        control = cancellation_db(m_rt, v_rt, i_rt)
        rows.append(
            {
                "arm": arm,
                "measured_cancellation_db": round(measured, 2),
                "own_content_control_db": round(control, 2),
                "gap_db": round(control - measured, 2),
            }
        )
        print(f"  {arm:>22}  measured {measured:6.2f} dB   control {control:6.2f} dB   gap {control-measured:+6.2f} dB")

    ratings = json.loads(
        (BENCH / "ratings" / "ratings-zeno-signs-musdb18-hq-pop-rock-sung-male-lead-cheap-floor-ladder-20260724T104946Z.json").read_text()
    )
    scored = [r for r in ratings["ratings"] if r["human_score"] is not None]
    kept = [r for r in scored if r["id"] != FAILED_ARM]
    agreement = {
        "with_failed_arm": {
            "n": len(scored),
            "spearman": round(spearman([r["human_score"] for r in scored], [r["si_sdr"] for r in scored]), 3),
        },
        "without_failed_arm": {
            "n": len(kept),
            "spearman": round(spearman([r["human_score"] for r in kept], [r["si_sdr"] for r in kept]), 3),
            "si_sdr_span_db": round(max(r["si_sdr"] for r in kept) - min(r["si_sdr"] for r in kept), 2),
            "distinct_human_scores": sorted({r["human_score"] for r in kept}),
        },
    }
    top = [r for r in ratings["ratings"] if r["human_score"] == 8]
    agreement["top_cluster"] = {
        "n_arms_tied_at_8": len(top),
        "si_sdr_span_db": round(max(r["si_sdr"] for r in top) - min(r["si_sdr"] for r in top), 2),
        "project_inaudible_threshold_db": 0.2,
    }
    print(f"  agreement: {json.dumps(agreement, indent=2)}")

    OUT.write_text(json.dumps({"cancellation_controls": rows, "agreement": agreement}, indent=2) + "\n")
    print(f"[done] wrote {OUT}")


if __name__ == "__main__":
    main()
