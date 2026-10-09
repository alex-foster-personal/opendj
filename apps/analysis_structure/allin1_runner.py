#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = [
#     "all-in-one-mps==1.0.0",
#     "natten-mps==0.3.3",
#     "torch==2.8.0",
#     "torchaudio==2.8.0",
#     "numpy==2.4.6",
#     "soundfile==0.14.0",
# ]
# ///
"""All-In-One section boundaries and labels, one JSON document per track.

WHICH PACKAGE AND WHY. Upstream `allin1` needs the `natten` CUDA/C++
extension (a version that no longer ships the functional API allin1 calls)
and `madmom` from git, which does not build on Python 3.11 plus numpy 1.24 or
later without patching. `all-in-one-mps` is an MIT fork of the same code and
the same MIT weights (`taejunkim/allinone`, model `harmonix-all`) whose
neighborhood attention comes from `natten-mps`, a pure PyTorch
implementation with a Metal fast path. The pure path runs on CPU and CUDA
unchanged, so one script serves the Mac, a Linux CPU box and an NVIDIA card.
It installs under the import name `allin1`. Measured on a 4-core Linux CPU,
Thu 24 Sep 2026: 136 s for a 217 s track (demucs 82 s, network 38 s).

WHAT IS KEPT. Only segments (start, end, label). The model's beats and
downbeats are written too, for diagnosis, but the app never serves them: the
bar quantizer (`apps/analysis_structure/quantize.py`) snaps boundaries onto
the downbeats of the track's own beatgrid lane, because Raveform Table 4
measured a Harmonix-trained All-In-One at downbeat F1 0.753 on EDM, below the
grid we already have.

ENERGY ENVELOPES FOR THE DJ LABELS. allin1 separates the track with Demucs
before it segments. With ``keep_byproducts=True`` the stems land in
``<demix_dir>/htdemucs/<audio file stem>/{bass,drums,other,vocals}.wav``
(all-in-one-mps 1.0.0 ``allin1/demix.py``; with ``keep_byproducts=False``
it ignores ``demix_dir`` and deletes its own temp dir before returning, so
the stems would be gone). The runner reads them once into RMS envelopes in dB
at a fixed hop (``drums``, ``bass`` and ``mix``, the sum of all four stems),
then deletes every byproduct itself, so nothing outlives the track. The
envelopes feed ``apps/analysis_structure/dj_labels.py``. A failure to compute
them never fails the track: ``envelopes`` is null and ``envelopes_error``
says why.

NOT BITWISE STABLE. Demucs runs first and seeding it does not make reruns
identical end to end, so each output records the package, torch and device it
ran with rather than claiming a rerun reproduces it.

Usage (from the repo root, heavy deps stay out of the app venv):

    uv run --no-project --script apps/analysis_structure/allin1_runner.py \\
        --manifest tracks.jsonl --out out_dir [--device auto|cpu|cuda|mps]

`tracks.jsonl` holds one `{"stable_id": ..., "path": ...}` per line. Each
track writes `<out_dir>/<stable_id>.json`, with `status: failed` and the
exception text when that one track could not be analyzed; a failed track
never stops the batch and never writes an empty success.

-Claude
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import json
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

MODEL = "harmonix-all"
HOP_S = 0.1  # envelope frame; 10 frames a second keeps a 6 min track near 3600 values per lane
FLOOR_DB = -120.0  # digital silence is clamped here instead of -inf
STEMS = ("bass", "drums", "other", "vocals")


def _device(requested: str) -> str:
    import torch

    if requested != "auto":
        return requested
    if torch.cuda.is_available():  # ROCm builds also answer here
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _versions(device: str) -> dict[str, str]:
    import torch

    return {
        "all-in-one-mps": md.version("all-in-one-mps"),
        "natten-mps": md.version("natten-mps"),
        "torch": torch.__version__,
        "model": MODEL,
        "device": device,
    }


def _rms_db(x: Any, hop: int) -> list[float]:
    import numpy as np

    n = len(x) // hop
    frames = np.asarray(x[: n * hop], dtype=np.float64).reshape(n, hop)
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    db = 20.0 * np.log10(np.maximum(rms, 10.0 ** (FLOOR_DB / 20.0)))
    return [round(float(v), 1) for v in db]


def envelopes(stem_dir: Path, hop_s: float = HOP_S) -> dict[str, Any]:
    """RMS envelopes in dB of the drums, the bass and the full mix (sum of all stems)."""
    import soundfile as sf

    mono: dict[str, Any] = {}
    rates = set()
    for name in STEMS:
        data, rate = sf.read(stem_dir / f"{name}.wav", dtype="float32", always_2d=True)
        mono[name] = data.mean(axis=1)
        rates.add(rate)
    if len(rates) != 1:
        raise ValueError(f"stems disagree on sample rate: {sorted(rates)}")
    hop = round(rates.pop() * hop_s)
    mix = sum(mono[name] for name in STEMS)
    return {
        "hop_s": hop_s,
        "unit": "dBFS_rms",
        "drums": _rms_db(mono["drums"], hop),
        "bass": _rms_db(mono["bass"], hop),
        "mix": _rms_db(mix, hop),
    }


def _envelopes_or_error(stem_dir: Path) -> dict[str, Any]:
    try:
        return {"envelopes": envelopes(stem_dir), "envelopes_error": None}
    except Exception as exc:  # noqa: BLE001 -- the labels are optional, the sections are not
        return {"envelopes": None, "envelopes_error": f"{type(exc).__name__}: {exc}"}


def _analyze_one(path: Path, device: str, work: Path) -> dict[str, Any]:
    import allin1  # type: ignore[import-not-found]

    t0 = time.perf_counter()
    try:
        r = allin1.analyze(
            str(path),
            out_dir=None,
            model=MODEL,
            device=device,
            demucs_device=device,
            demix_dir=str(work / "demix"),
            spec_dir=str(work / "spec"),
            keep_byproducts=True,  # the stems are read below, then deleted here
            multiprocess=False,
        )
        env = _envelopes_or_error(work / "demix" / "htdemucs" / path.stem)
    finally:
        # STRUCT-03: the stems must not stay on disk. A failed delete fails the
        # track loudly (its reason names the dir) rather than passing as ok.
        shutil.rmtree(work, ignore_errors=True)
        if work.exists():
            raise RuntimeError(f"could not delete Demucs byproducts in {work}")
    return {
        "status": "ok",
        "segments": [
            {"start": float(s.start), "end": float(s.end), "label": str(s.label)}
            for s in r.segments
        ],
        "model_bpm": r.bpm,
        "model_beats": [float(x) for x in r.beats],
        "model_downbeats": [float(x) for x in r.downbeats],
        **env,
        "seconds": round(time.perf_counter() - t0, 3),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--device", default="auto", choices=("auto", "cpu", "cuda", "mps"))
    args = ap.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    device = _device(args.device)
    versions = _versions(device)
    tracks = [json.loads(line) for line in args.manifest.read_text().splitlines() if line.strip()]
    failed = 0
    with tempfile.TemporaryDirectory(prefix="allin1-") as tmp:
        for i, t in enumerate(tracks):
            path = Path(t["path"])
            doc: dict[str, Any] = {
                "stable_id": t["stable_id"],
                "path": str(path),
                "versions": versions,
            }
            try:
                if not path.is_file():
                    raise FileNotFoundError(f"audio not found: {path}")  # noqa: TRY301
                # A fresh work dir per track: allin1 reuses any stems it finds
                # under the same file stem, which two tracks can share.
                doc.update(_analyze_one(path, device, Path(tmp) / f"t{i}"))
            except Exception as exc:  # noqa: BLE001 -- one bad file must not end the batch
                failed += 1
                doc.update(
                    {
                        "status": "failed",
                        "reason": f"{type(exc).__name__}: {exc}",
                        "traceback": traceback.format_exc(limit=5),
                    }
                )
            (args.out / f"{t['stable_id']}.json").write_text(json.dumps(doc))
            print(
                json.dumps(
                    {
                        "stable_id": t["stable_id"],
                        "status": doc["status"],
                        "seconds": doc.get("seconds"),
                    }
                ),
                flush=True,
            )
    print(json.dumps({"tracks": len(tracks), "failed": failed, **versions}), file=sys.stderr)
    return 1 if failed == len(tracks) and tracks else 0


if __name__ == "__main__":
    raise SystemExit(main())
