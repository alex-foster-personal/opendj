#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx>=0.27",
# ]
# ///
"""Probe paid stem APIs (LALAL.AI / AudioShake / Moises) into comparable WAVs.

Env keys (never commit; put in repo ``.env``):
  LALAL_API_KEY
  AUDIOSHAKE_API_KEY
  MOISES_API_KEY

Usage:
  uv run tools/stem-api-probe/probe.py --provider lalal --input PATH --out DIR
  uv run tools/stem-api-probe/probe.py --provider audioshake --input PATH --out DIR
  uv run tools/stem-api-probe/probe.py --provider moises --input PATH --out DIR

Sources adapted from public docs:
  LALAL: https://www.lalal.ai/api/ (license key header; task create + poll)
  AudioShake: https://developer.audioshake.ai/
  Moises: partner API varies; stub fails loud if undocumented for this key

SDR note (for humans):
  "Under 1 dB" on 4-stem tables means mean Source-to-Distortion Ratio differs
  by less than one decibel. That is an energy-error average, NOT crispness,
  metallic artifact rate, transient smear, or "works on hard EDM splits"
  success rate. Prefer listening rubrics + hard-split hit rate for DJ mute use.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import httpx

PROVIDERS = ("lalal", "audioshake", "moises")


def _require_key(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(
            f"error: set {name} in the environment / .env "
            "(signup pages were opened in Chrome for credits)"
        )
    return value


def _save_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


# ----- LALAL.AI --------------------------------------------------------------
# Docs: https://www.lalal.ai/api/  (Authorization: license <key>)


def run_lalal(audio: Path, out: Path) -> dict:
    key = _require_key("LALAL_API_KEY")
    headers = {"Authorization": f"license {key}"}
    with httpx.Client(timeout=120.0) as client:
        with audio.open("rb") as fh:
            up = client.post(
                "https://www.lalal.ai/api/upload/",
                headers=headers,
                files={"file": (audio.name, fh)},
            )
        up.raise_for_status()
        upload = up.json()
        file_id = upload.get("id") or upload.get("file_id") or upload.get("upload_id")
        if not file_id:
            raise RuntimeError(f"lalal upload missing id: {upload}")
        # Split into multiple stems in one task when supported by the account.
        task = client.post(
            "https://www.lalal.ai/api/task/",
            headers=headers,
            data={
                "id": file_id,
                "stem": "vocals,bass,drums,other",
            },
        )
        # Some accounts use /api/split/ or JSON body; surface raw errors.
        if task.status_code >= 400:
            task = client.post(
                "https://www.lalal.ai/api/split/",
                headers={**headers, "Content-Type": "application/json"},
                json={"id": file_id, "stem": "vocals"},
            )
        task.raise_for_status()
        meta = {"upload": upload, "task": task.json()}
        (out / "lalal_meta.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )
        # Polling shapes differ by plan; store response for the human to finish wiring.
        return meta


# ----- AudioShake ------------------------------------------------------------


def run_audioshake(audio: Path, out: Path) -> dict:
    key = _require_key("AUDIOSHAKE_API_KEY")
    # Public docs: https://developer.audioshake.ai/ - exact paths vary by contract.
    base = os.environ.get("AUDIOSHAKE_API_BASE", "https://groovy.audioshake.ai").rstrip(
        "/"
    )
    headers = {"Authorization": f"Bearer {key}"}
    with httpx.Client(timeout=180.0) as client:
        with audio.open("rb") as fh:
            resp = client.post(
                f"{base}/api/v1/jobs",
                headers=headers,
                files={"file": (audio.name, fh)},
                data={"stems": "vocals,drums,bass,other"},
            )
        if resp.status_code >= 400:
            raise SystemExit(
                f"audioshake HTTP {resp.status_code}: {resp.text[:800]}\n"
                "Check developer.audioshake.ai for the current job create path "
                "for your contract; probe left intentionally fail-loud."
            )
        meta = resp.json()
        (out / "audioshake_meta.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )
        return meta


# ----- Moises ----------------------------------------------------------------


def run_moises(audio: Path, out: Path) -> dict:
    key = os.environ.get("MOISES_API_KEY", "").strip()
    if not key:
        raise SystemExit(
            "error: Moises public developer API is not consistently documented.\n"
            "  Set MOISES_API_KEY if you have partner credentials, else use the "
            "desktop/app export path for A/B.\n"
            "  Signup: https://moises.ai/  Pricing: https://moises.ai/pricing"
        )
    base = os.environ.get("MOISES_API_BASE", "").rstrip("/")
    if not base:
        raise SystemExit(
            "error: set MOISES_API_BASE to your partner API host "
            "(Moises does not publish a stable public stem REST URL for all accounts)"
        )
    headers = {"Authorization": f"Bearer {key}"}
    with httpx.Client(timeout=180.0) as client:
        with audio.open("rb") as fh:
            resp = client.post(
                f"{base}/v1/stems",
                headers=headers,
                files={"file": (audio.name, fh)},
            )
        resp.raise_for_status()
        meta = resp.json()
        (out / "moises_meta.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8"
        )
        return meta


def main() -> int:
    p = argparse.ArgumentParser(description="Paid stem API probe")
    p.add_argument("--provider", required=True, choices=PROVIDERS)
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    if not args.input.is_file():
        raise SystemExit(f"error: input missing: {args.input}")
    args.out.mkdir(parents=True, exist_ok=True)
    runners = {
        "lalal": run_lalal,
        "audioshake": run_audioshake,
        "moises": run_moises,
    }
    meta = runners[args.provider](args.input, args.out)
    print(
        json.dumps(
            {"provider": args.provider, "out": str(args.out), "meta": meta}, indent=2
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
