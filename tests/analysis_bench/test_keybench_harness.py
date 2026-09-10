"""Key-lane harness + S-KEY adapter, no torch and no production library."""

from __future__ import annotations

import json
import math
import struct
import wave
from pathlib import Path

import pytest

import numpy as np

from apps.analysis_key import canon, profiles
from scripts.keybench import _harness
from scripts.keybench.run_krumhansl import result_from_estimate
from scripts.keybench.run_skey import build_arm_from_skey_raw, main as skey_main


def _write_wav(path: Path, seconds: float = 2.0, freq: float = 261.63) -> None:
    rate = 44100
    n = int(seconds * rate)
    with wave.open(str(path), "w") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        frames = bytearray()
        for i in range(n):
            value = int(12000 * math.sin(2 * math.pi * freq * i / rate))
            frames += struct.pack("<h", value)
        fh.writeframes(bytes(frames))


def test_harness_honors_paths_relative_to_manifest(tmp_path: Path) -> None:
    staged = tmp_path / "bundle"
    wav_dir = staged / "wav"
    wav_dir.mkdir(parents=True)
    wav = wav_dir / "sid-1.wav"
    _write_wav(wav)
    (staged / "manifest.json").write_text(
        json.dumps(
            {
                "paths_relative_to": "manifest",
                "fixtures": [{"stable_id": "sid-1", "wav": "wav/sid-1.wav"}],
            }
        ),
        encoding="utf-8",
    )
    seen: list[str] = []

    def analyzer(path: str) -> dict:
        seen.append(path)
        return {
            "key_camelot": "8B",
            "key_openkey": "1d",
            "error": None,
        }

    class Args:
        fixtures = str(staged / "manifest.json")
        out = str(tmp_path / "arm.json")
        limit = 0

    assert _harness.run(
        Args(), candidate="stub", version="test", device="cpu",
        build_analyzer=lambda: analyzer,
    ) == 0
    assert seen == [str(wav)]
    payload = json.loads(Path(Args.out).read_text())
    assert payload["n_fixtures"] == 1
    assert payload["n_failed"] == 0
    assert payload["results"]["sid-1"]["key_camelot"] == "8B"


def test_harness_refuses_unknown_paths_relative_to(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"paths_relative_to": "cwd", "fixtures": []}),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="paths_relative_to"):
        _harness.resolve_fixture_paths(
            json.loads(manifest.read_text()), str(manifest)
        )


def test_throwing_analyzer_is_an_error_not_a_dropped_id(tmp_path: Path) -> None:
    staged = tmp_path / "bundle"
    staged.mkdir()
    (staged / "manifest.json").write_text(
        json.dumps(
            {
                "paths_relative_to": "manifest",
                "fixtures": [
                    {"stable_id": "ok", "wav": "missing-ok.wav"},
                    {"stable_id": "boom", "wav": "missing-boom.wav"},
                ],
            }
        ),
        encoding="utf-8",
    )

    def analyzer(path: str) -> dict:
        if path.endswith("missing-boom.wav"):
            raise RuntimeError("nope")
        return {"key_camelot": "8A", "key_openkey": "1m", "error": None}

    class Args:
        fixtures = str(staged / "manifest.json")
        out = str(tmp_path / "arm.json")
        limit = 0

    assert _harness.run(
        Args(), candidate="stub", version="test", device="cpu",
        build_analyzer=lambda: analyzer,
    ) == 0
    payload = json.loads(Path(Args.out).read_text())
    assert set(payload["results"]) == {"ok", "boom"}
    assert payload["n_failed"] == 1
    assert payload["results"]["boom"]["error"].startswith("RuntimeError")
    assert payload["results"]["ok"]["error"] is None


def test_krumhansl_result_emits_both_notations_canon_agrees() -> None:
    chroma = np.zeros((12, 8))
    # C major triad: C, E, G
    chroma[0] = 1.0
    chroma[4] = 0.5
    chroma[7] = 0.7
    estimate = profiles.estimate_key_krumhansl(chroma)
    row = result_from_estimate(estimate)
    key = canon.from_camelot(row["key_camelot"])
    assert canon.from_open_key(row["key_openkey"]) == key
    assert row["key_camelot"] == canon.to_camelot(estimate.key)
    assert row["key_openkey"] == canon.to_open_key(estimate.key)
    assert row["error"] is None


def test_skey_adapter_remaps_paths_and_converts_notations(tmp_path: Path) -> None:
    wav = tmp_path / "tone.wav"
    _write_wav(wav)
    raw = {
        "schema": 1,
        "skey_revision": "918b83d273568d5041569bb8068843d19a335726",
        "torch_version": "2.7.1",
        "device": "cpu",
        "model_load_s": 0.05,
        "checkpoint": {"sha256": "abc"},
        "results": {
            str(wav): {
                "audio": str(wav),
                "label": "A minor",
                "key_rekordbox_style": "Am",
                "inference_s": 1.2,
                "error": None,
            }
        },
    }
    raw_path = tmp_path / "skey-raw.json"
    raw_path.write_text(json.dumps(raw), encoding="utf-8")
    # Adapter walks the bundle fixtures; hand it a one-row list instead.
    fixtures_one = [{"stable_id": "synthetic-agree-1", "wav": str(wav)}]
    arm = build_arm_from_skey_raw(raw, fixtures_one)
    assert arm["candidate"] == "skey"
    assert arm["device"] == "cpu"
    assert arm["skey_revision"] == "918b83d273568d5041569bb8068843d19a335726"
    row = arm["results"]["synthetic-agree-1"]
    assert row["error"] is None
    assert row["key_camelot"] == "8A"
    assert row["key_openkey"] == "1m"
    assert row["runtime_s"] == 1.2
    assert "key_confidence" not in row
    assert "no_tonal_center" not in row

    out = tmp_path / "skey.json"
    manifest = tmp_path / "one.json"
    manifest.write_text(
        json.dumps({"fixtures": fixtures_one, "paths_relative_to": "manifest"}),
        encoding="utf-8",
    )
    assert skey_main(
        ["--fixtures", str(manifest), "--out", str(out), "--skey-raw", str(raw_path)]
    ) == 0
    written = json.loads(out.read_text())
    assert written["candidate"] == "skey"
    assert written["results"]["synthetic-agree-1"]["key_openkey"] == "1m"
