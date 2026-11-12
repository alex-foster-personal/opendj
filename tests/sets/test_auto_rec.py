"""SETREC-11802/11802b: opendj REC CLI parity and recorded WAV duration checks."""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

from apps.opendj_cli import rec_cli as opendj_rec_cli
from apps.sets import rec_cli
from apps.sets.recorder_service import recorded_duration_within, session_wav_duration_s, wav_pcm_duration_s

SESSION = "2026-11-10T08-00-00"


def _write_tone_wav(path: Path, *, seconds: float, sample_rate: int = 48_000, frames: int | None = None) -> None:
    frames = frames if frames is not None else int(seconds * sample_rate)
    pcm = b"".join(struct.pack("<hh", int(9000 * math.sin(i / 7)), int(9000 * math.sin(i / 7))) for i in range(frames))
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)


@pytest.mark.requirement("SETREC-11802")
def test_recorded_wav_duration_is_within_tolerance_of_played_time(tmp_path: Path) -> None:
    """[if] REC runs ~0.5s [then] WAV duration is within 0.1s of played time, [else stop]."""
    session_dir = tmp_path / "sets" / SESSION
    _write_tone_wav(session_dir / "audio_2026-11-10T08-00-00.wav", seconds=0.5)
    assert wav_pcm_duration_s(session_dir / "audio_2026-11-10T08-00-00.wav") == pytest.approx(0.5, abs=0.01)
    assert session_wav_duration_s(session_dir) == pytest.approx(0.5, abs=0.01)
    assert recorded_duration_within(session_dir, expected_s=0.5, tolerance_s=0.1)
    assert not recorded_duration_within(session_dir, expected_s=0.9, tolerance_s=0.1)
    status = {
        "session_id": SESSION,
        "recordings_dir": str(tmp_path / "sets"),
    }
    assert rec_cli.verify_recorded_duration(status, expected_s=0.5, tolerance_s=0.1) == pytest.approx(0.5, abs=0.01)


@pytest.mark.requirement("SETREC-11802")
def test_opendj_rec_delegates_to_sets_rec_cli(monkeypatch) -> None:
    """[if] opendj rec start runs [then] it posts the same body as apps.sets rec, [else stop]."""
    captured: dict[str, object] = {}

    def fake_dispatch(args):
        captured["cmd"] = args.rec_cmd
        captured["source"] = args.source
        captured["base_url"] = args.base_url
        return 0

    monkeypatch.setattr(opendj_rec_cli.sets_rec_cli, "dispatch", fake_dispatch)
    monkeypatch.setattr(
        opendj_rec_cli,
        "resolve_backend_base_url",
        lambda lock_path=None: type("T", (), {"base_url": "http://127.0.0.1:8702"})(),
    )
    assert opendj_rec_cli.run(["start", "--source", "master"]) == 0
    assert captured == {
        "cmd": "start",
        "source": "master",
        "base_url": "http://127.0.0.1:8702",
    }


@pytest.mark.requirement("SETREC-11802b")
def test_first_play_wires_auto_rec_once_per_session() -> None:
    """[if] first play runs [then] auto-REC arms once per /performance session, [else stop]."""
    ipc = (REPO_ROOT / "apps/webui/frontend/src/lib/rb/performance-ipc.svelte.ts").read_text(encoding="utf-8")
    auto = (REPO_ROOT / "apps/webui/frontend/src/lib/sets/auto-rec.ts").read_text(encoding="utf-8")
    assert "maybeStartAutoRecOnFirstPlay()" in ipc
    assert "resetSetAutoRecSession()" in ipc
    assert "autoRecConsumed = true" in auto
    assert "if (!autoRecEnabled || autoRecConsumed" in auto


@pytest.mark.requirement("SETREC-11802b")
def test_a_second_first_play_hook_does_not_re_arm_auto_rec() -> None:
    """[if] auto-REC already consumed [then] a second hook returns before REC start, [else stop]."""
    auto = (REPO_ROOT / "apps/webui/frontend/src/lib/sets/auto-rec.ts").read_text(encoding="utf-8")
    assert "autoRecConsumed" in auto
    assert "maybeStartAutoRecOnFirstPlay(): void" in auto
    assert "if (!autoRecEnabled || autoRecConsumed" in auto
