# /// script
# requires-python = ">=3.10"
# dependencies = ["soundfile>=0.12", "numpy<2", "scipy>=1.10"]
# ///
"""R1: red-team attack on two claims from the Zeno - Signs listening round.

GPU-free, network-free, spend-free. Reads only files already on disk.

Claim A under attack: "hdemucs_mmi ov0 scored instrumental 10 while ov25 scored 7, so
overlap 0 has a better instrumental". If the instrumental clip is just mixture minus the
vocal estimate, then the instrumental carries NO information the vocal clip does not, and
a 10-vs-7 split on identical vocal 8s is a rating artefact, not a signal.
  Test: cancellation depth of mixture - (vocal_est + inst_est), in dB.
  Positive control: a KNOWN exact subtraction pushed through the same AAC encode, which
  bounds how much cancellation the codec alone permits.

Claim B under attack: "SI-SDR disagrees with the maintainer's ears, so SI-SDR is the wrong metric".
Three rival explanations, tested against each other:
  H1 bandwidth - the disagreeing arms are band-limited, and SI-SDR charges full price for
     a missing top octave the ear barely notices on vocals.
  H2 delay - a resample round trip shifted the estimate, and SI-SDR is delay-intolerant.
  H3 no instrument fault - the arms really are worse and the ear is the outlier.

Instrument validation runs FIRST and gates everything: this script's SI-SDR must reproduce
the published ladder numbers on lossless files before any of its own numbers are believed.

Requirements (mini-PRD):
  ✔︎ ✅ SI-SDR here reproduces ladder_musdb.json to within 0.05 dB on lossless FLAC.
    [if] max abs deviation > 0.05 dB [then ⛔️] RuntimeError, analysis does not run
  ✔︎ ✅ cancellation depth reported for lossless rungs AND the rated m4a arms, each next
    to a same-pipeline exact-subtraction control.
    [if] a mixture/vocal/inst trio is missing a file [then ⛔️] RuntimeError naming it
  ✔︎ ✅ H1/H2/H3 separated by re-scoring under band-matching and under lag correction.
    [if] neither correction moves the disagreeing arms [then] H3 stands and attack B dies

Run:
  uv run scripts/bench/r1-offline/r1_inst_and_metric.py

-Claude
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import signal

REPO = Path(__file__).resolve().parents[3]
BENCH = REPO / "scripts" / "bench"
OUT = Path(__file__).resolve().parent / "r1.json"

# Band split for H1. Spleeter's published architecture stops at 11.025 kHz and a 22.05 kHz
# separation cannot carry anything above 11.025 kHz either, so that is the natural cut.
BAND_HZ: float = 11025.0
# Cross-correlation search for H2, +/- 50 ms is far wider than any resampler delay.
MAX_LAG_S: float = 0.05
# Instrument validation tolerance.
VALIDATE_TOL_DB: float = 0.05


# ----- loading ----------------------------------------------------------------


def _load_mono(path: Path) -> tuple[np.ndarray, int]:
    if not path.is_file():
        raise RuntimeError(f"audio file does not exist: {path}")
    data, sr = sf.read(str(path), dtype="float64", always_2d=True)
    return data.mean(axis=1), int(sr)


def _load_mono_any(path: Path) -> tuple[np.ndarray, int]:
    """FLAC/WAV direct, m4a via ffmpeg to a temp wav (soundfile cannot decode AAC)."""
    if path.suffix.lower() in {".flac", ".wav"}:
        return _load_mono(path)
    if not path.is_file():
        raise RuntimeError(f"audio file does not exist: {path}")
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav = Path(tmp.name)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-i", str(path), "-c:a", "pcm_f32le", str(wav)],
        check=True,
    )
    try:
        return _load_mono(wav)
    finally:
        wav.unlink(missing_ok=True)


def _align(*sigs: np.ndarray) -> list[np.ndarray]:
    n = min(len(s) for s in sigs)
    if n == 0:
        raise RuntimeError("zero-length overlap")
    return [s[:n] for s in sigs]


# ----- metrics ----------------------------------------------------------------


def si_sdr_db(reference: np.ndarray, estimate: np.ndarray, zero_mean: bool) -> float:
    ref, est = _align(reference, estimate)
    if zero_mean:
        ref = ref - ref.mean()
        est = est - est.mean()
    alpha = float(np.dot(est, ref) / np.dot(ref, ref))
    target = alpha * ref
    noise = est - target
    return float(10.0 * np.log10(np.dot(target, target) / np.dot(noise, noise)))


def cancellation_db(mixture: np.ndarray, vocal: np.ndarray, inst: np.ndarray) -> float:
    """How completely vocal_est + inst_est reconstructs the mixture. Higher = more exact."""
    mix, voc, ins = _align(mixture, vocal, inst)
    residual = mix - (voc + ins)
    return float(10.0 * np.log10(np.dot(mix, mix) / max(np.dot(residual, residual), 1e-30)))


def band_energy_fraction(x: np.ndarray, sr: int, cut_hz: float) -> float:
    """Fraction of total energy above cut_hz. Near zero = band-limited."""
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    freqs = np.fft.rfftfreq(len(x), 1.0 / sr)
    total = spec.sum()
    return float(spec[freqs > cut_hz].sum() / total) if total > 0 else 0.0


def lowpass(x: np.ndarray, sr: int, cut_hz: float) -> np.ndarray:
    sos = signal.butter(8, cut_hz / (sr / 2.0), btype="low", output="sos")
    return signal.sosfiltfilt(sos, x)


def best_lag(reference: np.ndarray, estimate: np.ndarray, sr: int) -> int:
    """Integer lag (samples) maximising cross-correlation, estimate relative to reference."""
    ref, est = _align(reference, estimate)
    max_lag = int(MAX_LAG_S * sr)
    corr = signal.correlate(est, ref, mode="full")
    centre = len(ref) - 1
    window = corr[centre - max_lag : centre + max_lag + 1]
    return int(np.argmax(np.abs(window)) - max_lag)


def shift(x: np.ndarray, lag: int) -> np.ndarray:
    if lag == 0:
        return x
    if lag > 0:
        return np.concatenate([x[lag:], np.zeros(lag)])
    return np.concatenate([np.zeros(-lag), x[:lag]])


def spearman(a: list[float], b: list[float]) -> float:
    ra, rb = _rank(a), _rank(b)
    ra, rb = np.asarray(ra), np.asarray(rb)
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
        mean_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = mean_rank
        i = j + 1
    return ranks


# ----- step 0: validate the instrument ----------------------------------------


def validate_si_sdr() -> dict:
    """Reproduce published ladder_musdb.json SI-SDR on lossless FLAC, or refuse to continue."""
    ladder = json.loads((BENCH / "ladder_musdb.json").read_text())
    rows: list[dict] = []
    for track in ladder["tracks"]:
        ref, ref_sr = _load_mono(BENCH / track["reference"])
        for clip in track["clips"]:
            est, est_sr = _load_mono(BENCH / clip["file"])
            if est_sr != ref_sr:
                raise RuntimeError(f"sample-rate mismatch on {clip['file']}")
            rows.append(
                {
                    "file": clip["file"],
                    "published": clip["si_sdr"],
                    "zero_mean": round(si_sdr_db(ref, est, zero_mean=True), 3),
                    "raw": round(si_sdr_db(ref, est, zero_mean=False), 3),
                }
            )
    for variant in ("zero_mean", "raw"):
        worst = max(abs(r[variant] - r["published"]) for r in rows)
        if worst <= VALIDATE_TOL_DB:
            return {"variant": variant, "max_abs_dev_db": round(worst, 4), "n": len(rows), "rows": rows}
    devs = {v: round(max(abs(r[v] - r["published"]) for r in rows), 3) for v in ("zero_mean", "raw")}
    raise RuntimeError(
        f"SI-SDR here does not reproduce the published ladder (max dev {devs}); "
        "instrument is unvalidated, refusing to run the analysis"
    )


# ----- step 1: is the instrumental an independent measurement -------------------


@dataclass
class Trio:
    label: str
    mixture: Path
    vocal: Path
    inst: Path


def cancellation_table(trios: list[Trio]) -> list[dict]:
    rows: list[dict] = []
    for t in trios:
        mix, mix_sr = _load_mono_any(t.mixture)
        voc, voc_sr = _load_mono_any(t.vocal)
        ins, ins_sr = _load_mono_any(t.inst)
        if not (mix_sr == voc_sr == ins_sr):
            raise RuntimeError(f"sample-rate mismatch in trio {t.label}")
        rows.append({"label": t.label, "cancellation_db": round(cancellation_db(mix, voc, ins), 2)})
    return rows


def aac_floor_control(mixture: Path, vocal: Path, bitrate: str) -> dict:
    """Positive control: build inst = mixture - vocal EXACTLY, encode both to AAC at the
    same bitrate as the rated clips, and measure cancellation. This is the ceiling any
    true subtraction can reach once the codec has had its way with it."""
    mix, sr = _load_mono_any(mixture)
    voc, voc_sr = _load_mono_any(vocal)
    if sr != voc_sr:
        raise RuntimeError("control sample-rate mismatch")
    mix, voc = _align(mix, voc)
    ins = mix - voc
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        paths = {}
        for name, sig in (("mix", mix), ("voc", voc), ("ins", ins)):
            wav = tmp / f"{name}.wav"
            sf.write(str(wav), sig, sr, subtype="FLOAT")
            m4a = tmp / f"{name}.m4a"
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-i", str(wav), "-c:a", "aac", "-b:a", bitrate, str(m4a)],
                check=True,
            )
            paths[name] = m4a
        dm, _ = _load_mono_any(paths["mix"])
        dv, _ = _load_mono_any(paths["voc"])
        di, _ = _load_mono_any(paths["ins"])
        return {
            "label": "CONTROL exact subtraction, lossless",
            "cancellation_db_lossless": round(cancellation_db(mix, voc, ins), 2),
            "label_coded": f"CONTROL exact subtraction, AAC {bitrate}",
            "cancellation_db_aac": round(cancellation_db(dm, dv, di), 2),
        }


# ----- step 2: why does SI-SDR disagree with the ear ---------------------------


def metric_diagnosis(ratings_path: Path) -> dict:
    ratings = json.loads(ratings_path.read_text())
    truth, truth_sr = _load_mono_any(BENCH / "clips-cheap" / "zeno-signs-truth-vocals.m4a")
    truth_lp = lowpass(truth, truth_sr, BAND_HZ)

    rows: list[dict] = []
    for r in ratings["ratings"]:
        est_path = BENCH / r["file"]
        est, sr = _load_mono_any(est_path)
        if sr != truth_sr:
            raise RuntimeError(f"sample-rate mismatch on {r['file']}")
        ref, e = _align(truth, est)
        base = si_sdr_db(ref, e, zero_mean=True)
        lag = best_lag(ref, e, sr)
        lag_fixed = si_sdr_db(ref, shift(e, lag), zero_mean=True)
        ref_lp, e_lp = _align(truth_lp, lowpass(e, sr, BAND_HZ))
        band_fixed = si_sdr_db(ref_lp, e_lp, zero_mean=True)
        both = si_sdr_db(ref_lp, shift(e_lp, best_lag(ref_lp, e_lp, sr)), zero_mean=True)
        rows.append(
            {
                "id": r["id"],
                "human_score": r["human_score"],
                "human_inst_score": r["human_inst_score"],
                "published_si_sdr": r["si_sdr"],
                "si_sdr_m4a": round(base, 3),
                "lag_samples": lag,
                "si_sdr_lag_corrected": round(lag_fixed, 3),
                "si_sdr_band_matched": round(band_fixed, 3),
                "si_sdr_band_and_lag": round(both, 3),
                "energy_above_11k_frac": round(band_energy_fraction(e, sr, BAND_HZ), 6),
            }
        )
    rows.append(
        {
            "id": "_truth_reference",
            "energy_above_11k_frac": round(band_energy_fraction(truth, truth_sr, BAND_HZ), 6),
        }
    )

    scored = [r for r in rows if r.get("human_score") is not None]
    agreement = {
        "n": len(scored),
        "spearman_human_vs_si_sdr": round(
            spearman([r["human_score"] for r in scored], [r["si_sdr_m4a"] for r in scored]), 3
        ),
        "spearman_human_vs_lag_corrected": round(
            spearman([r["human_score"] for r in scored], [r["si_sdr_lag_corrected"] for r in scored]), 3
        ),
        "spearman_human_vs_band_matched": round(
            spearman([r["human_score"] for r in scored], [r["si_sdr_band_matched"] for r in scored]), 3
        ),
        "spearman_human_vs_band_and_lag": round(
            spearman([r["human_score"] for r in scored], [r["si_sdr_band_and_lag"] for r in scored]), 3
        ),
    }
    return {"rows": rows, "agreement": agreement}


# ----- main -------------------------------------------------------------------


def main() -> None:
    result: dict = {}

    print("[0] validating SI-SDR against published ladder numbers on lossless FLAC ...")
    validation = validate_si_sdr()
    print(f"    OK variant={validation['variant']} max_dev={validation['max_abs_dev_db']} dB over n={validation['n']}")
    result["instrument_validation"] = validation

    print("[1] cancellation depth: does inst_est + vocal_est reconstruct the mixture ...")
    lossless = [
        Trio(
            label=f"lossless musdb {name} {rung:02d}",
            mixture=BENCH / f"clips-musdb/{name}-mixture.flac",
            vocal=BENCH / f"clips-musdb/{name}-rung-{rung:02d}.flac",
            inst=BENCH / f"clips-musdb/{name}-rung-{rung:02d}.inst.flac",
        )
        for name in ("zeno-signs", "al-james-schoolboy-facination")
        for rung in (1, 5, 10)
    ]
    rated = [
        Trio(
            label=f"rated m4a {arm}",
            mixture=BENCH / "clips-cheap/zeno-signs-mixture.m4a",
            vocal=BENCH / f"clips-cheap/zeno-signs-{arm}.m4a",
            inst=BENCH / f"clips-cheap/zeno-signs-{arm}.inst.m4a",
        )
        for arm in (
            "hdemucs_mmi-ov0",
            "hdemucs_mmi-ov25",
            "htdemucs-ov25",
            "rb7-stems",
            "rb6-spleeter",
            "rb6-spleeter-mono-22k",
            "htdemucs-ov0-22k",
            "floor-midside",
        )
    ]
    result["cancellation"] = {
        "lossless": cancellation_table(lossless),
        "rated_m4a": cancellation_table(rated),
        "control": aac_floor_control(
            BENCH / "clips-cheap/zeno-signs-mixture.m4a",
            BENCH / "clips-cheap/zeno-signs-truth-vocals.m4a",
            "244k",
        ),
    }
    for row in result["cancellation"]["lossless"] + result["cancellation"]["rated_m4a"]:
        print(f"    {row['label']:>44}  {row['cancellation_db']:>8.2f} dB")
    print(f"    control: {result['cancellation']['control']}")

    print("[2] why SI-SDR disagrees with the ear: bandwidth vs delay vs neither ...")
    result["metric_diagnosis"] = metric_diagnosis(
        BENCH / "ratings" / "ratings-zeno-signs-musdb18-hq-pop-rock-sung-male-lead-cheap-floor-ladder-20260724T104946Z.json"
    )
    for row in result["metric_diagnosis"]["rows"]:
        print(
            f"    {row['id']:>22}  human={row.get('human_score')}  "
            f"si_sdr={row.get('si_sdr_m4a')}  lag={row.get('lag_samples')}  "
            f"band_matched={row.get('si_sdr_band_matched')}  "
            f"hf_frac={row.get('energy_above_11k_frac')}"
        )
    print(f"    agreement: {result['metric_diagnosis']['agreement']}")

    OUT.write_text(json.dumps(result, indent=2) + "\n")
    print(f"[done] wrote {OUT}")


if __name__ == "__main__":
    main()
