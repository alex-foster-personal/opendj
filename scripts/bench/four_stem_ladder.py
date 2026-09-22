# /// script
# requires-python = ">=3.11"
# dependencies = ["torch", "demucs", "onnxruntime", "numpy<2", "soundfile", "scipy", "fast-bss-eval>=0.1.4"]
# ///
"""Four-stem listening set: every config emits vocals, drums, bass AND other.

Every earlier run in this repo scored VOCALS only. The live-mashup product needs
drums, bass and other too, so a config that is fine on vocals and poor on bass is
a product problem nobody had looked for. This runs each config once, keeps all
four of its stems, and scores each against its TRUE MUSDB18-HQ counterpart.

Design rules this script enforces, because the last listening round tripped on
all three:
  - The raw mixture is NOT a graded arm. It is scored per stem as context only
    (the do-nothing floor) and lands in the manifest as a reference.
  - The TRUE stem for each source is kept and shipped as the reference, so an
    instrumental or a drum stem is never graded blind against nothing.
  - Every graded arm is a real separator output.

Configs are demucs checkpoints plus the rekordbox 7 STEMS ONNX graph, which is
the shipping bar. The rekordbox arm reuses rekordbox_stems.separate so there is
one implementation of that convention, not two.

Requirements (mini-PRD):
  ✔︎ ✅ 🎯 emit 4 stems per config at the input rate and channel count
    [if] a config returns fewer than 4 sources [then ⛔️] RuntimeError naming it
    [if] an output length differs from the mixture [then ⛔️] RuntimeError
  ✔︎ ✅ 🎯 SI-SDR per (config, stem) against the TRUE stem, plus the mixture floor
    [if] a truth stem file is missing [then ⛔️] RuntimeError, no silent skip
    [if] truth and estimate sample rates differ [then ⛔️] RuntimeError, no resample
  ✔︎ ✅ 🎯 write one JSON with the full config x stem matrix
    [if] any cell is absent [then ⛔️] RuntimeError before the JSON is written

Run:
  uv run scripts/bench/four_stem_ladder.py \
    --window-dir /path/to/window --out-dir .tmp/bench/4stem \
    --rb7-model /path/to/hdemucs.onnx --json-out scripts/bench/four_stem_ladder.json

-Claude
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Any

import fast_bss_eval.numpy as fast_bss_eval_numpy
import numpy as np
import rekordbox_stems
import soundfile as sf
import torch
from demucs.apply import apply_model
from demucs.pretrained import get_model
from scipy.signal import resample_poly

_HERE: Path = Path(__file__).resolve().parent

STEMS: tuple[str, ...] = ("vocals", "drums", "bass", "other")

# Params strings are deliberately FLAT. The previous ladder shipped labels like
# "THIS IS THE BAR" and "EXPECTED TOO BAD" next to the score chips, and the four
# arms whose labels said they were good all came back tied at 8/10. A label that
# tells the rater the answer is not a measurement.
#
# Arm choice is a spread, not a cluster. Three arms inside 0.6 dB spends rating
# slots inside a range this project already calls inaudible (0.2 dB), so the two
# cheap-lever arms below are here to open the range: they are real separator
# outputs, several dB down, and both are shipping engines rather than strawmen.
DEMUCS_ARMS: tuple[dict[str, Any], ...] = (
    {"id": "hdemucs_mmi-ov0", "model": "hdemucs_mmi", "overlap": 0.0,
     "sep_rate": 44100, "params": "hdemucs_mmi, overlap 0, 44.1 kHz"},
    {"id": "hdemucs_mmi-ov25", "model": "hdemucs_mmi", "overlap": 0.25,
     "sep_rate": 44100, "params": "hdemucs_mmi, overlap 0.25, 44.1 kHz"},
    {"id": "htdemucs-ov25", "model": "htdemucs", "overlap": 0.25,
     "sep_rate": 44100, "params": "htdemucs, overlap 0.25, 44.1 kHz"},
    {"id": "htdemucs-ov0-22k", "model": "htdemucs", "overlap": 0.0,
     "sep_rate": 22050, "params": "htdemucs, overlap 0, separated at 22.05 kHz"},
)
RB7_ARM: dict[str, Any] = {
    "id": "rb7-stems",
    "params": "rekordbox 7 STEMS, its own hdemucs.onnx, overlap 0.25, 44.1 kHz",
}
RB6_ARM: dict[str, Any] = {
    "id": "rb6-spleeter",
    "params": "rekordbox 6 Track Separation, its own Spleeter 4stems weights, 44.1 kHz",
}


def _load(path: Path) -> tuple[torch.Tensor, int]:
    if not path.is_file():
        raise RuntimeError(f"audio file does not exist: {path}")
    audio, sr = sf.read(str(path), dtype="float32", always_2d=True)
    return torch.from_numpy(audio.T), int(sr)


def _si_sdr_db(reference: np.ndarray, estimate: np.ndarray) -> float:
    n = min(len(reference), len(estimate))
    if n == 0:
        raise RuntimeError("zero-length overlap between reference and estimate")
    score = fast_bss_eval_numpy.si_sdr(reference[:n][None, :], estimate[:n][None, :])
    return round(float(np.asarray(score).reshape(-1)[0]), 3)


def _mono(x: torch.Tensor) -> np.ndarray:
    return x.mean(dim=0).to(torch.float64).numpy()


#----- separators -------------------------------------------------------------


def _resample(x: torch.Tensor, src: int, dst: int) -> torch.Tensor:
    if src == dst:
        return x
    g = np.gcd(src, dst)
    return torch.from_numpy(
        resample_poly(x.numpy(), dst // g, src // g, axis=-1).astype(np.float32))


def run_demucs_arm(model_name: str, mix: torch.Tensor, sr: int, *, overlap: float,
                   sep_rate: int) -> tuple[dict[str, torch.Tensor], float]:
    """Return {stem: (2, samples)} at the INPUT rate, plus inference seconds.

    A reduced sep_rate resamples down, separates, and resamples back up, and the
    resample sits inside the timed section because that cost is part of the arm.
    """
    model = get_model(model_name)
    model.eval()
    missing = [s for s in STEMS if s not in model.sources]
    if missing:
        raise RuntimeError(f"{model_name} has no sources {missing}, cannot emit 4 stems")

    n_in = mix.shape[-1]
    t0 = time.perf_counter()
    work = _resample(mix, sr, sep_rate)
    with torch.no_grad():
        out = apply_model(model, work.unsqueeze(0), overlap=overlap, shifts=0,
                          split=True, progress=False, device="cpu", num_workers=0)[0]
    stems = {s: _resample(out[model.sources.index(s)], sep_rate, sr)[..., :n_in]
             for s in STEMS}
    infer_s = time.perf_counter() - t0

    if out.shape[0] != len(model.sources):
        raise RuntimeError(f"{model_name} returned {out.shape[0]} sources, expected "
                           f"{len(model.sources)}")
    return stems, infer_s


def run_rb6_arm(mixture_wav: Path, work_dir: Path, sr: int
                ) -> tuple[dict[str, torch.Tensor], float]:
    """rekordbox 6's Spleeter 4stems engine, out of process.

    Spleeter needs tensorflow and demucs needs torch; resolving both into one
    environment is a fight with no upside, so this shells out to the existing
    single-purpose script and reads its four stem wavs back.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [_require_uv(), "run", "--python", "3.11", "--no-project",
         str(_HERE / "rekordbox6_spleeter.py"), "--input", str(mixture_wav),
         "--out-dir", str(work_dir), "--sep-rate", "44100", "--label", "rb6-spleeter"],
        capture_output=True, text=True, cwd=str(_HERE), check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"rekordbox6_spleeter.py failed:\n{proc.stderr[-3000:]}")
    report = json.loads(proc.stdout.strip().splitlines()[-1])

    stems: dict[str, torch.Tensor] = {}
    for stem in STEMS:
        audio, stem_sr = _load(work_dir / f"{stem}.wav")
        if stem_sr != sr:
            raise RuntimeError(f"spleeter {stem} came back at {stem_sr} Hz, expected {sr}")
        stems[stem] = audio
    return stems, float(report["infer_s"])


def _require_uv() -> str:
    import shutil
    path = shutil.which("uv")
    if not path:
        raise RuntimeError("uv is not on PATH, cannot run the out-of-process arms")
    return path


def run_rb7_arm(model_path: Path, mix: torch.Tensor, sr: int
                ) -> tuple[dict[str, torch.Tensor], float]:
    """rekordbox 7's own ONNX graph, via rekordbox_stems so the convention has
    exactly one implementation."""
    import onnxruntime as ort

    if sr != 44100:
        raise RuntimeError(f"rekordbox STEMS expects 44.1 kHz, got {sr}")
    if not model_path.is_file():
        raise RuntimeError(f"rekordbox ONNX model not found: {model_path}")
    sess = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
    stems, infer_s = rekordbox_stems.separate(sess, mix.unsqueeze(0))

    resid = (stems.sum(dim=0) - mix).abs().mean().item()
    scale = mix.abs().mean().item()
    if resid > 0.15 * scale:
        raise RuntimeError(
            f"rekordbox stems do not sum to the mixture: {resid / scale:.1%} residual, "
            "convention is wrong")
    order = rekordbox_stems.SOURCES
    return {s: stems[order.index(s)] for s in STEMS}, infer_s


def _load_truth_stems(window_dir: Path, mix: torch.Tensor, sr: int) -> dict[str, torch.Tensor]:
    truth: dict[str, torch.Tensor] = {}
    for stem in STEMS:
        stem_audio, stem_sr = _load(window_dir / f"{stem}.wav")
        if stem_sr != sr:
            raise RuntimeError(f"true {stem} is {stem_sr} Hz, mixture is {sr} Hz")
        if stem_audio.shape[-1] != mix.shape[-1]:
            raise RuntimeError(
                f"true {stem} is {stem_audio.shape[-1]} samples, mixture is {mix.shape[-1]}"
            )
        truth[stem] = stem_audio
    return truth


def _run_separation_arms(args, mix: torch.Tensor, sr: int) -> list[dict[str, Any]]:
    arms: list[dict[str, Any]] = []
    for spec in DEMUCS_ARMS:
        print(f"[run] {spec['id']}", flush=True)
        stems, infer_s = run_demucs_arm(
            spec["model"], mix, sr, overlap=spec["overlap"], sep_rate=spec["sep_rate"],
        )
        arms.append({**spec, "engine": "demucs", "infer_s": round(infer_s, 2), "_audio": stems})
    if args.rb7_model is not None:
        print(f"[run] {RB7_ARM['id']}", flush=True)
        stems, infer_s = run_rb7_arm(args.rb7_model, mix, sr)
        arms.append({
            **RB7_ARM, "engine": "rekordbox7-onnx", "model": "hdemucs.onnx",
            "overlap": rekordbox_stems.OVERLAP, "sep_rate": sr,
            "infer_s": round(infer_s, 2), "_audio": stems,
        })
    if args.with_rb6:
        print(f"[run] {RB6_ARM['id']}", flush=True)
        stems, infer_s = run_rb6_arm(args.window_dir / "mixture.wav", args.out_dir / "_rb6-work", sr)
        arms.append({
            **RB6_ARM, "engine": "rekordbox6-spleeter", "model": "spleeter_4stems",
            "overlap": 0.0, "sep_rate": sr, "infer_s": round(infer_s, 2), "_audio": stems,
        })
    return arms


#----- main -------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--window-dir", required=True, type=Path,
                    help="dir holding mixture.wav plus one wav per true stem")
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--rb7-model", type=Path, default=None,
                    help="rekordbox 7 hdemucs.onnx; omit to skip that arm")
    ap.add_argument("--with-rb6", action="store_true",
                    help="also run rekordbox 6's Spleeter 4stems, out of process")
    ap.add_argument("--json-out", required=True, type=Path)
    ap.add_argument("--track", required=True)
    ap.add_argument("--genre", required=True)
    ap.add_argument("--window-start-s", required=True, type=float)
    ap.add_argument("--window-length-s", required=True, type=float)
    args = ap.parse_args()

    mix, sr = _load(args.window_dir / "mixture.wav")
    truth = _load_truth_stems(args.window_dir, mix, sr)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    for stem, audio in truth.items():
        sf.write(str(args.out_dir / f"truth-{stem}.wav"), audio.T.numpy(), sr)
    sf.write(str(args.out_dir / "mixture.wav"), mix.T.numpy(), sr)

    arms = _run_separation_arms(args, mix, sr)
    duration_s = mix.shape[-1] / sr

    truth_mono = {s: _mono(t) for s, t in truth.items()}
    mix_mono = _mono(mix)
    mixture_floor = {s: _si_sdr_db(truth_mono[s], mix_mono) for s in STEMS}

    results: list[dict[str, Any]] = []
    for arm in arms:
        audio = arm.pop("_audio")
        scores: dict[str, float] = {}
        for stem in STEMS:
            est = audio[stem]
            if est.shape[-1] != mix.shape[-1]:
                raise RuntimeError(f"{arm['id']} {stem} is {est.shape[-1]} samples, "
                                   f"mixture is {mix.shape[-1]}")
            sf.write(str(args.out_dir / f"{arm['id']}-{stem}.wav"), est.T.numpy(), sr)
            scores[stem] = _si_sdr_db(truth_mono[stem], _mono(est))
        missing = [s for s in STEMS if s not in scores]
        if missing:
            raise RuntimeError(f"{arm['id']} has no score for {missing}")
        results.append({**arm,
                        "s_per_stem_minute": round(arm["infer_s"] / (duration_s / 60), 2),
                        "si_sdr": scores})

    payload = {
        "track": args.track,
        "genre": args.genre,
        "dataset": "MUSDB18-HQ test",
        "window": {"start_s": args.window_start_s, "length_s": args.window_length_s},
        "hardware": "Apple M3, CPU only",
        "stems": list(STEMS),
        "mixture_floor_si_sdr": mixture_floor,
        "arms": results,
    }
    args.json_out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    width = max(len(a["id"]) for a in results)
    header = "arm".ljust(width) + "".join(s.rjust(9) for s in STEMS) + "   s/stem-min"
    print(header)
    print("-" * len(header))
    for arm in results:
        row = arm["id"].ljust(width)
        row += "".join(f"{arm['si_sdr'][s]:9.2f}" for s in STEMS)
        row += f"{arm['s_per_stem_minute']:13.2f}"
        print(row)
    print("mixture floor".ljust(width) + "".join(f"{mixture_floor[s]:9.2f}" for s in STEMS))
    print(f"[OK] wrote {args.json_out}")


if __name__ == "__main__":
    main()
