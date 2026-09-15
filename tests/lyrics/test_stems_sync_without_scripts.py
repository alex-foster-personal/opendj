"""Packaged-engine parity: ``push_missing`` must not import ``scripts``."""

from __future__ import annotations

import importlib
import importlib.abc
import json
import math
import sys
import wave
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_wav(path: Path, seconds: float = 1.0) -> None:
    frames = int(seconds * 8000)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(
            b"".join(
                int(3000 * math.sin(i / 20)).to_bytes(2, "little", signed=True) * 2
                for i in range(frames)
            )
        )


class _RefuseScripts(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "scripts" or fullname.startswith("scripts."):
            raise ModuleNotFoundError(
                f"{fullname} is not shipped in the engine payload", name=fullname
            )
        return None


def test_push_missing_dry_run_without_scripts_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """if push_missing imports scripts at call time then the packaged engine 500s."""
    # Every change below is monkeypatch-owned so it is undone at teardown.
    # Inserting the finder into the live sys.meta_path leaked it into every
    # later test in the same worker: 25 unrelated tests then failed with
    # "scripts is not shipped in the engine payload" (trunk-red, Tue 15 Sep
    # 2026, #2410).
    for key in list(sys.modules):
        if key == "scripts" or key.startswith("scripts."):
            monkeypatch.delitem(sys.modules, key, raising=False)
    monkeypatch.delitem(sys.modules, "apps.lyrics.stems_sync", raising=False)
    monkeypatch.setattr(sys, "meta_path", [_RefuseScripts(), *sys.meta_path])

    data_dir = tmp_path / "data"
    external = tmp_path / "external-stems"
    external.mkdir()
    monkeypatch.setenv("MDT_EXTERNAL_STEM_ROOTS", str(external))
    stems_root = data_dir / "state" / "stems-roformer-spike" / "sid1"
    stems_root.mkdir(parents=True)
    _write_wav(stems_root / "vocals.wav")
    _write_wav(stems_root / "instrumental.wav")
    (stems_root / "manifest.json").write_text(
        json.dumps({
            "schema_version": 3,
            "stable_id": "sid1",
            "layout": "roformer2",
            "model": {"name": "m", "version": "v"},
            "source": {"path": "/x/a.wav", "sha256": "0" * 64},
            "files": {"vocals": "vocals.wav", "instrumental": "instrumental.wav"},
            "audio": {"sample_rate": 8000, "channels": 2, "frame_count": 8000},
        }),
        encoding="utf-8",
    )

    from apps.lyrics import stems_sync

    rc = stems_sync.push_missing(data_dir=data_dir, dry_run=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert "dry run: no credentials used, nothing uploaded" in out
