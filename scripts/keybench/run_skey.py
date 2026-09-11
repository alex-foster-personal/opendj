"""S-KEY adapter: bundle wavs in, scorer arm JSON out. No torch in this process.

The PEP 723 runner (`apps/analysis_key/skey_runner.py`) stays `apps/`-free and
path-keyed. This adapter resolves the sealed bundle, invokes that runner via
`uv run --no-project --script`, remaps `results[path]` onto `results[stable_id]`,
and converts `key_rekordbox_style` through `apps.analysis_key.canon`.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from apps.analysis_key import canon
from scripts.keybench._harness import resolve_fixture_paths

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUNNER = REPO_ROOT / "apps" / "analysis_key" / "skey_runner.py"


def _lookup_raw(raw_results: dict[str, Any], wav: str) -> dict[str, Any] | None:
    if wav in raw_results:
        return raw_results[wav]
    abs_wav = os.path.abspath(wav)
    if abs_wav in raw_results:
        return raw_results[abs_wav]
    for path, row in raw_results.items():
        if os.path.abspath(path) == abs_wav:
            return row
    return None


def convert_skey_row(row: dict[str, Any]) -> dict[str, Any]:
    """One skey-runner result -> one arm result. No fabricated confidence.

    S-KEY does not emit a calibrated confidence, so this adapter omits
    `key_confidence` and `no_tonal_center`. A missing confidence is fine;
    `== 0.0` is the only confidence check the scorer makes.
    """
    error = row.get("error")
    if error:
        return {
            "error": error,
            "runtime_s": row.get("inference_s"),
            "skey_label": row.get("label"),
        }
    scale = row.get("key_rekordbox_style")
    if not scale:
        return {
            "error": "skey runner returned no key_rekordbox_style",
            "runtime_s": row.get("inference_s"),
            "skey_label": row.get("label"),
        }
    try:
        key = canon.from_rekordbox_scale_name(str(scale))
    except ValueError as exc:
        return {
            "error": f"unparseable skey key_rekordbox_style {scale!r}: {exc}"[:300],
            "runtime_s": row.get("inference_s"),
            "skey_label": row.get("label"),
        }
    return {
        "key_camelot": canon.to_camelot(key),
        "key_openkey": canon.to_open_key(key),
        "runtime_s": row.get("inference_s"),
        "error": None,
        "skey_label": row.get("label"),
        "key_rekordbox_style": scale,
    }


def build_arm_from_skey_raw(
    raw: dict[str, Any],
    fixtures: list[dict[str, Any]],
) -> dict[str, Any]:
    """Remap a path-keyed skey-runner payload onto stable_id-keyed arm JSON."""
    raw_results = raw.get("results") or {}
    results: dict[str, Any] = {}
    n_failed = 0
    for fixture in fixtures:
        stable_id = fixture["stable_id"]
        wav = fixture["wav"]
        row = _lookup_raw(raw_results, wav)
        if row is None:
            converted = {
                "error": "skey runner omitted this fixture",
                "runtime_s": None,
            }
        else:
            converted = convert_skey_row(row)
        if converted.get("error"):
            n_failed += 1
        results[stable_id] = converted

    checkpoint = raw.get("checkpoint") or {}
    return {
        "schema": 1,
        "candidate": "skey",
        "candidate_version": (
            f"skey@{raw.get('skey_revision') or raw.get('producer_version') or 'unknown'}"
        ),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "n_fixtures": len(fixtures),
        "n_failed": n_failed,
        "device": raw.get("device") or "cpu",
        "skey_revision": raw.get("skey_revision"),
        "torch_version": raw.get("torch_version"),
        "torchaudio_version": raw.get("torchaudio_version"),
        "checkpoint_sha256": checkpoint.get("sha256"),
        "model_load_s": raw.get("model_load_s"),
        "results": results,
    }


def _invoke_skey_runner(
    *,
    wavs: list[str],
    out: Path,
    device: str,
    runner: Path,
) -> dict[str, Any]:
    if not wavs:
        raise SystemExit("[skey-adapter] bundle has no wav paths to analyze")
    cmd = [
        "uv", "run", "--no-project", "--script", str(runner),
        "--device", device,
        "--skip-onnx-export",
        "--audio", *wavs,
        "--out", str(out),
    ]
    print(f"[skey-adapter] {' '.join(cmd)}", flush=True)
    completed = subprocess.run(cmd, check=False)
    if completed.returncode != 0:
        raise SystemExit(
            f"[skey-adapter] skey_runner.py exited {completed.returncode}"
        )
    if not out.is_file():
        raise SystemExit(f"[skey-adapter] skey_runner.py wrote no {out}")
    return json.loads(out.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--device", default="cpu", choices=("cpu", "mps", "cuda", "auto"))
    parser.add_argument(
        "--skey-runner",
        default=str(DEFAULT_RUNNER),
        help="path to apps/analysis_key/skey_runner.py",
    )
    parser.add_argument(
        "--skey-raw",
        help="reuse an already-written skey_runner JSON instead of invoking uv (tests)",
    )
    args = parser.parse_args(argv)

    with open(args.fixtures, encoding="utf-8") as fh:
        manifest = json.load(fh)
    fixtures = resolve_fixture_paths(manifest, args.fixtures)

    if args.skey_raw:
        raw = json.loads(Path(args.skey_raw).read_text(encoding="utf-8"))
    else:
        raw_out = Path(args.out).with_suffix(".skey-raw.json")
        wavs = [str(f["wav"]) for f in fixtures if f.get("wav")]
        raw = _invoke_skey_runner(
            wavs=wavs,
            out=raw_out,
            device=args.device,
            runner=Path(args.skey_runner),
        )

    payload = build_arm_from_skey_raw(raw, fixtures)
    Path(args.out).write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print(
        f"[skey-adapter] {payload['n_fixtures']} fixtures, "
        f"{payload['n_failed']} failed, device={payload['device']} -> {args.out}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
