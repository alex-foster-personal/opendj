# /// script
# requires-python = ">=3.12"
# dependencies = ["numpy>=1.26", "soundfile>=0.12"]
# ///
"""Stem vocal-coverage for ANY corpus with a <id>-vocals / <id>-instrumental dir.

One implementation of the measurement the no-lyrics verdict is banded from
(apps/lyrics/vocal_presence.py holds the bands; this holds the number).
Replaces the two corpus-specific copies that existed for oltf and novox.

The maths is not re-derived here: scripts/vocal_region_worker.py's
ratio_envelope + regions_payload are loaded by path (the same trick
apps/vocals/from_stems.py uses) so this file, the library vocal-cache and the
bench pages can never disagree about what coverage means. mix = vocals stem +
instrumental stem, because the separation is additive.

  --envelopes additionally emits DISPLAY_COLS peak-downsampled columns of each
  envelope (ints 0-1000, scaled by the track's own mix peak) plus the region
  spans and a peak_vox_s jump target -- everything the bench listening widget
  needs to draw a seekable waveform.

Usage (repo root):
    uv run --script scripts/lyrics_stem_coverage.py \
        --stems data/state/lyrics-eval/own-crate/stems \
        --out data/state/lyrics-eval/own-crate/vocal-presence.json --envelopes

-Claude
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import numpy as np
import soundfile as sf

REPO_ROOT: Path = Path(__file__).resolve().parents[1]
WORKER_PATH: Path = REPO_ROOT / "scripts" / "vocal_region_worker.py"
DISPLAY_COLS: int = 400
AUDIO_SUFFIXES: tuple[str, ...] = (".flac", ".mp3", ".wav", ".m4a", ".ogg")


def _worker_mod() -> ModuleType:
    spec = importlib.util.spec_from_file_location("vocal_region_worker", WORKER_PATH)
    if spec is None or spec.loader is None:
        raise SystemExit(f"[ERROR] cannot load worker module from {WORKER_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _read_mono(path: Path) -> tuple[np.ndarray, int]:
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    return data.mean(axis=1), int(sr)


def _rms(mono: np.ndarray, sr: int, hop_s: float) -> np.ndarray:
    hop = int(sr * hop_s)
    n = len(mono) // hop
    if n == 0:
        raise SystemExit(f"[ERROR] audio shorter than one hop ({hop} frames @ {sr} Hz)")
    framed = mono[: n * hop].reshape(n, hop)
    return np.sqrt((framed * framed).mean(axis=1))


def _columns(env: np.ndarray, scale: float) -> list[int]:
    """Downsample by PEAK, never mean: a mean hides the short vocal phrase that
    decides a sparse track, which is the whole population this measures."""
    if len(env) == 0:
        return [0] * DISPLAY_COLS
    idx = np.linspace(0, len(env), DISPLAY_COLS + 1).astype(int)
    cols = [
        float(env[a:b].max()) if b > a else float(env[min(a, len(env) - 1)])
        for a, b in zip(idx[:-1], idx[1:], strict=True)
    ]
    return [int(min(1000, round(1000 * c / scale))) for c in cols]


def _pairs(stems_dir: Path) -> list[tuple[str, Path, Path]]:
    """(track_id, vocals, instrumental) for every complete bundle in the dir.

    A HALF bundle raises: silently featuring 'whatever paired up' is how a
    denominator quietly shrinks.
    """
    vocals: dict[str, Path] = {}
    instr: dict[str, Path] = {}
    for p in sorted(stems_dir.iterdir()):
        if p.suffix.lower() not in AUDIO_SUFFIXES:
            continue
        if p.stem.endswith("-vocals"):
            vocals[p.stem[: -len("-vocals")]] = p
        elif p.stem.endswith("-instrumental"):
            instr[p.stem[: -len("-instrumental")]] = p
    orphans = set(vocals) ^ set(instr)
    if orphans:
        raise SystemExit(f"[ERROR] {len(orphans)} half stem bundle(s) in {stems_dir}: "
                         f"{sorted(orphans)[:5]}")
    return [(tid, vocals[tid], instr[tid]) for tid in sorted(vocals)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stems", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--envelopes", action="store_true",
                    help="also emit display columns + region spans for the bench widget")
    args = ap.parse_args()
    if not args.stems.is_dir():
        raise SystemExit(f"[ERROR] stems dir {args.stems} does not exist")

    worker = _worker_mod()
    hop_s = float(worker.HOP_S)
    pairs = _pairs(args.stems)
    if not pairs:
        raise SystemExit(f"[ERROR] no stem bundles found in {args.stems}")
    rows: list[dict] = []
    for i, (tid, vocals_path, instr_path) in enumerate(pairs, 1):
        vocals, sr = _read_mono(vocals_path)
        instr, sr_i = _read_mono(instr_path)
        if sr != sr_i:
            raise SystemExit(f"[ERROR] {tid}: stem sample rates differ ({sr} vs {sr_i})")
        n = min(len(vocals), len(instr))
        mix = vocals[:n] + instr[:n]
        v_env = _rms(vocals[:n], sr, hop_s)
        m_env = _rms(mix, sr, hop_s)
        k = min(len(v_env), len(m_env))
        v_env, m_env = v_env[:k], m_env[:k]
        duration_s = n / float(sr)
        ratio = worker.ratio_envelope(v_env.tolist(), m_env.tolist(), hop_s)
        payload = worker.regions_payload(ratio, duration_s=duration_s, hop_s=hop_s)
        regions = payload["regions"]
        row = {
            "track_id": tid,
            "coverage_pct": payload["coverage_pct"],
            "vocal_s": round(sum(r["end_s"] - r["start_s"] for r in regions), 1),
            "duration_s": round(duration_s, 1),
            "n_regions": len(regions),
            "thresholds_adapted": payload["thresholds_adapted"],
            "stem_vocals": vocals_path.name,
        }
        if args.envelopes:
            scale = float(m_env.max()) or 1.0
            row["env_mix"] = _columns(m_env, scale)
            row["env_vox"] = _columns(v_env, scale)
            row["regions"] = [[round(r["start_s"], 1), round(r["end_s"], 1)] for r in regions]
            if regions:
                peak_s = int(np.argmax(v_env)) * hop_s
                holder = next((r for r in regions if r["start_s"] <= peak_s <= r["end_s"]),
                              max(regions, key=lambda r: r["end_s"] - r["start_s"]))
                row["peak_vox_s"] = round((holder["start_s"] + holder["end_s"]) / 2, 1)
            else:
                row["peak_vox_s"] = None
        rows.append(row)
        if i % 20 == 0 or i == len(pairs):
            print(f"[..] {i}/{len(pairs)} {args.stems.name}", flush=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    print(f"[OK] {args.out} -- {len(rows)} tracks (denominator: {len(pairs)} stem bundles "
          f"in {args.stems})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
