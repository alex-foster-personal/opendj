"""The payload ships odj-audio with device output (NAE-13, plan 20-02).

Regression lines:
- if the launcher does not point ODJ_AUDIO_BIN at the payload's own build then
  broken: a Tauri- or CLI-launched payload would find no engine, or a repo one
- if a build without ``--features device`` can be staged then broken: it boots,
  passes every other check, and fails only when a DJ turns the Rust engine on
- if a binary that prints no hello, or a hello of another protocol, is staged
  then broken
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.build_engine_payload import (
    AUDIO_ENGINE_RELATIVE,
    PAYLOAD_LAUNCHER_PREAMBLE,
    PayloadBuildError,
    verify_audio_engine,
)
from tests.rust_build_env import build_audio_engine

REPO_ROOT = Path(__file__).resolve().parents[2]


def _fake_engine(tmp_path: Path, stdout: str) -> Path:
    binary = tmp_path / "odj-audio"
    binary.write_text(f"#!/bin/sh\ncat <<'OUT'\n{stdout}\nOUT\n", encoding="utf-8")
    binary.chmod(0o755)
    return binary


def _hello(**overrides: object) -> str:
    hello = {
        "type": "hello",
        "protocol": 1,
        "engine": "odj-audio 0.1.0",
        "clock": "fake",
        "sample_rate": 48000,
        "decks": 4,
        "device": True,
    }
    hello.update(overrides)
    return json.dumps(hello)


def test_the_launcher_points_the_engine_at_the_payload_build(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    (payload / "bin").mkdir(parents=True)
    probe = payload / "bin/probe"
    probe.write_text(
        "#!/bin/sh\n" + PAYLOAD_LAUNCHER_PREAMBLE + 'printf "%s" "$ODJ_AUDIO_BIN"\n',
        encoding="utf-8",
    )
    probe.chmod(0o755)
    # An inherited value (a repo build) must not survive into the payload.
    out = subprocess.run(
        [str(probe)],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/bin:/bin", "ODJ_AUDIO_BIN": "/elsewhere/odj-audio"},
    )
    assert out.stdout == str((payload / AUDIO_ENGINE_RELATIVE).resolve())


def test_a_device_build_is_accepted(tmp_path: Path) -> None:
    hello = verify_audio_engine(_fake_engine(tmp_path, _hello()))
    assert hello["device"] is True
    assert hello["protocol"] == 1


@pytest.mark.parametrize(
    ("stdout", "why"),
    [
        (_hello(device=False), "device=False"),
        (_hello(device=None).replace(', "device": null', ""), "device=None"),
        (_hello(protocol=2), "not a protocol 1 hello"),
        ("", "printed no hello"),
    ],
)
def test_anything_else_is_refused(tmp_path: Path, stdout: str, why: str) -> None:
    with pytest.raises(PayloadBuildError, match=why):
        verify_audio_engine(_fake_engine(tmp_path, stdout))


def test_the_real_engine_reports_its_build() -> None:
    """The gate reads a field the real binary prints, not one only fakes carry."""
    # Rebuilt every run so a stale binary from before the field can't answer.
    built = build_audio_engine(REPO_ROOT / "apps/audio-engine")
    # The default build has no device feature, and says so (False, not absent).
    with pytest.raises(PayloadBuildError, match="device=False"):
        verify_audio_engine(built)
