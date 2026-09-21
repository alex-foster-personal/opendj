"""End-to-end portability tests for scripts/vocal_region_worker.py.

Windows portability plan section 3.4: WAV inputs must decode via soundfile
(never ffmpeg), non-WAV inputs must fail fast with an actionable message
when ffmpeg is unreachable, and the existing drift guard must still catch a
genuine source/resampled duration mismatch. These run the worker as a real
``uv run`` subprocess (its actual invocation contract) rather than
importing torch into the repo venv, so this module must NOT require torch
itself -- only ``uv`` on PATH and network/cache access for the PEP 723 env
(the htdemucs checkpoint is expected to already be cached locally; these
are marked ``slow`` like the rest of the model-touching suite).

Regression one-liners:
  - if a WAV input still shells to ffmpeg then broken (Windows has none)
  - if a non-WAV input silently mis-decodes when ffmpeg is absent then broken
  - if MDT_FFMPEG isn't honoured as a PATH override then broken
  - if the drift guard stops raising on a forced duration mismatch then broken
  - if MDT_VOCAL_WORKER_PYTHON isn't used over ``uv run`` when set then broken
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from apps.vocals import cli as vocals_cli

pytestmark = pytest.mark.requirement("CAT-05")

WORKER_SCRIPT: Path = (
    Path(__file__).resolve().parents[2] / "scripts" / "vocal_region_worker.py"
)

sf = pytest.importorskip("soundfile")
np = pytest.importorskip("numpy")

_UV: str | None = shutil.which("uv")


def _require_uv() -> str:
    if _UV is None:
        pytest.skip("uv not found on PATH")
    assert _UV is not None
    return _UV


def _isolated_path_dir(tmp_path: Path) -> Path:
    """A PATH containing ONLY a copy of the uv executable -- simulates a
    Windows box with no ffmpeg anywhere on PATH."""
    uv = _require_uv()
    isolated = tmp_path / "isolated_path"
    isolated.mkdir()
    target = isolated / Path(uv).name
    shutil.copy2(uv, target)
    target.chmod(0o755)
    return isolated


def _write_wav(path: Path, duration_s: float = 1.5, sr: int = 44100) -> None:
    n = int(duration_s * sr)
    t = np.linspace(0, duration_s, n, endpoint=False)
    tone = (0.1 * np.sin(2 * np.pi * 220 * t)).astype("float32")
    sf.write(str(path), np.stack([tone, tone], axis=1), sr)


def _write_aiff(path: Path, duration_s: float = 1.0, sr: int = 44100) -> None:
    """A non-WAV fixture that soundfile can write WITHOUT ffmpeg, so this
    module has no ffmpeg dependency of its own even to build fixtures.

    Non-silent on purpose: an all-zero clip drives demucs' internal
    normalisation (``(wav - ref.mean()) / ref.std()``) to a 0/0 NaN, which
    trips an unrelated assertion deep in htdemucs -- nothing to do with the
    portability behaviour under test here."""
    n = int(duration_s * sr)
    t = np.linspace(0, duration_s, n, endpoint=False)
    tone = (0.1 * np.sin(2 * np.pi * 220 * t)).astype("float32")
    sf.write(str(path), np.stack([tone, tone], axis=1), sr, format="AIFF")


# ----- import-without-torch contract (worker_units.py owns the full suite;
# ----- this is a light guard specific to the portability acceptance list) --

def test_import_is_torch_free() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "vocal_region_worker_portability_check", WORKER_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert "torch" not in sys.modules
    assert "demucs" not in sys.modules


# ----- WAV fast-path: no ffmpeg needed --------------------------------------

@pytest.mark.slow
def test_wav_fastpath_runs_without_ffmpeg_on_path(tmp_path: Path) -> None:
    uv = _require_uv()
    isolated = _isolated_path_dir(tmp_path)
    wav = tmp_path / "tiny.wav"
    _write_wav(wav)

    env = dict(os.environ)
    env["PATH"] = str(isolated)
    env.pop("MDT_FFMPEG", None)

    proc = subprocess.run(
        [uv, "run", "--script", str(WORKER_SCRIPT), "--device", "cpu", str(wav)],
        cwd=WORKER_SCRIPT.parents[1],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    assert shutil.which("ffmpeg", path=env["PATH"]) is None
    import json

    payload = json.loads(proc.stdout)
    assert "regions" in payload
    assert payload["source_sample_rate"] == 44100


# ----- non-WAV: fail fast when ffmpeg is unreachable ------------------------

@pytest.mark.slow
def test_nonwav_without_ffmpeg_raises_actionable_error(tmp_path: Path) -> None:
    uv = _require_uv()
    isolated = _isolated_path_dir(tmp_path)
    aiff = tmp_path / "tiny.aiff"
    _write_aiff(aiff)

    env = dict(os.environ)
    env["PATH"] = str(isolated)
    env.pop("MDT_FFMPEG", None)

    proc = subprocess.run(
        [uv, "run", "--script", str(WORKER_SCRIPT), "--device", "cpu", str(aiff)],
        cwd=WORKER_SCRIPT.parents[1],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=180,
    )
    assert proc.returncode != 0
    assert str(aiff) in proc.stderr
    assert "MDT_FFMPEG" in proc.stderr
    assert "pre-transcode" in proc.stderr


@pytest.mark.slow
def test_mdt_ffmpeg_env_override_is_honoured(tmp_path: Path) -> None:
    """When ffmpeg exists only at a nonstandard location, MDT_FFMPEG (the
    path to the executable itself) must make it reachable."""
    system_ffmpeg = shutil.which("ffmpeg")
    system_ffprobe = shutil.which("ffprobe")
    if system_ffmpeg is None or system_ffprobe is None:
        pytest.skip("no system ffmpeg+ffprobe available to relocate for this test")
    assert system_ffmpeg is not None and system_ffprobe is not None
    uv = _require_uv()
    isolated = _isolated_path_dir(tmp_path)

    # demucs.audio.AudioFile shells to BOTH ffmpeg and ffprobe, so a real
    # install always ships them side by side; relocate both so MDT_FFMPEG's
    # "prepend its parent dir" contract is satisfied like a real drop.
    relocated_dir = tmp_path / "relocated_ffmpeg"
    relocated_dir.mkdir()
    relocated_ffmpeg = relocated_dir / Path(system_ffmpeg).name
    shutil.copy2(system_ffmpeg, relocated_ffmpeg)
    relocated_ffmpeg.chmod(0o755)
    relocated_ffprobe = relocated_dir / Path(system_ffprobe).name
    shutil.copy2(system_ffprobe, relocated_ffprobe)
    relocated_ffprobe.chmod(0o755)

    aiff = tmp_path / "tiny2.aiff"
    _write_aiff(aiff)

    env = dict(os.environ)
    env["PATH"] = str(isolated)
    env["MDT_FFMPEG"] = str(relocated_ffmpeg)

    proc = subprocess.run(
        [uv, "run", "--script", str(WORKER_SCRIPT), "--device", "cpu", str(aiff)],
        cwd=WORKER_SCRIPT.parents[1],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr


# ----- drift guard: forced mismatch still raises ----------------------------

_DRIFT_HARNESS = textwrap.dedent(
    """\
    #!/usr/bin/env -S uv run
    # /// script
    # requires-python = ">=3.11,<3.13"
    # dependencies = [
    #   "demucs==4.0.1",
    #   "torch==2.5.1",
    #   "torchaudio==2.5.1",
    #   "soundfile>=0.12",
    #   "numpy<2",
    # ]
    # ///
    import importlib.util
    import sys
    from pathlib import Path

    worker_path, audio_path = sys.argv[1], sys.argv[2]
    spec = importlib.util.spec_from_file_location("vocal_region_worker", worker_path)
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)

    import torch

    def _fake_read(audio_path, model):
        # Wildly disagree with the tensor's real length -- forces the
        # existing (verbatim, untouched) drift guard to trip.
        return 44100, 999.0, torch.zeros(2, 44100 * 3)

    worker._read_wav_fastpath = _fake_read

    try:
        worker.analyse(Path(audio_path), "cpu")
    except RuntimeError as exc:
        print("DRIFT_RAISED:" + str(exc))
        sys.exit(0)
    else:
        print("NO_RAISE")
        sys.exit(1)
    """
)


@pytest.mark.slow
def test_drift_guard_still_raises_on_forced_mismatch(tmp_path: Path) -> None:
    uv = _require_uv()
    harness = tmp_path / "drift_harness.py"
    harness.write_text(_DRIFT_HARNESS, encoding="utf-8")
    wav = tmp_path / "any.wav"
    _write_wav(wav, duration_s=1.0)

    proc = subprocess.run(
        [uv, "run", str(harness), str(WORKER_SCRIPT), str(wav)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    assert "DRIFT_RAISED" in proc.stdout
    assert "drift" in proc.stdout.lower()


# ----- apps.vocals.cli.run_worker: interpreter override (hermetic, no --------
# ----- real subprocess -- covers the Windows farm-out interpreter contract) --

class _FakeWorkerProcess:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self.pid = 12345

    def communicate(self, timeout: float | None = None) -> tuple[str, None]:
        return self._stdout, None


def test_run_worker_uses_uv_run_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_popen(cmd: list[str], **kwargs: Any) -> _FakeWorkerProcess:
        captured["cmd"] = cmd
        return _FakeWorkerProcess(0, '{"ok": true}')

    monkeypatch.delenv("MDT_VOCAL_WORKER_PYTHON", raising=False)
    monkeypatch.delenv("MDT_VOCAL_WORKER_DEVICE", raising=False)
    monkeypatch.setattr(vocals_cli.subprocess, "Popen", fake_popen)

    result = vocals_cli.run_worker(Path("/tmp/track.wav"))
    assert result == {"ok": True}
    assert captured["cmd"][:2] == ["uv", "run"]
    assert "--device" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--device") + 1] == "auto"
    assert captured["cmd"][-1] == "/tmp/track.wav"


def test_run_worker_uses_interpreter_override_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_popen(cmd: list[str], **kwargs: Any) -> _FakeWorkerProcess:
        captured["cmd"] = cmd
        return _FakeWorkerProcess(0, '{"ok": true}')

    monkeypatch.setenv("MDT_VOCAL_WORKER_PYTHON", "/bench/python")
    monkeypatch.setenv("MDT_VOCAL_WORKER_DEVICE", "cuda")
    monkeypatch.setattr(vocals_cli.subprocess, "Popen", fake_popen)

    vocals_cli.run_worker(Path("/tmp/track.wav"))
    assert captured["cmd"][0] == "/bench/python"
    assert "uv" not in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--device") + 1] == "cuda"
