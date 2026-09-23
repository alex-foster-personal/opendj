"""Regression tests for the self-contained installed-app vocal worker.

[if] packaged env names a runtime and script [then] vocals launches exactly those, [else stop].
[if] the packaged runtime or script cannot start [then] reinstall guidance only, [else stop].
[if] a non-WAV track has no reachable ffmpeg [then] refuse it before launch, [else stop].

No fakes: every launch below goes through the real ``subprocess.Popen`` in a
child interpreter whose environment is passed explicitly, so the command
construction, the OS launch failure and the exception translation are the
production ones. Audio inputs are the committed, locked phase7-dedup media
fixtures. The controls assert the guards do not overshoot: a present script,
a WAV input, or a reachable ffmpeg must still reach the worker itself.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from apps.vocals import cli as vocals_cli

pytestmark = pytest.mark.requirement("INSTALL-27")

REPO_ROOT = Path(__file__).resolve().parents[2]
WAV_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "phase7-dedup" / "src.wav"
MP3_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "phase7-dedup" / "src-128.mp3"
_WORKER_ENV_KEYS = (
    vocals_cli.PACKAGED_WORKER_PYTHON_ENV,
    vocals_cli.PACKAGED_WORKER_SCRIPT_ENV,
    vocals_cli.FFMPEG_OVERRIDE_ENV,
)

_CHILD_RUN_WORKER = """
import json, sys
from pathlib import Path
from apps.vocals import cli
try:
    cli.run_worker(Path(sys.argv[1]), timeout_s=300)
except Exception as exc:
    cause = exc.__cause__
    print(json.dumps({
        "type": type(exc).__name__,
        "message": str(exc),
        "cause": type(cause).__name__ if cause is not None else None,
    }))
else:
    print(json.dumps({"type": None, "message": "", "cause": None}))
"""


def _child_env(**overrides: str) -> dict[str, str]:
    """This process's environment minus the worker knobs, plus ``overrides``."""
    env = {k: v for k, v in os.environ.items() if k not in _WORKER_ENV_KEYS}
    env.update(overrides)
    return env


def _empty_path_dir(tmp_path: Path) -> str:
    """A real directory to use as PATH that contains no ffmpeg."""
    bare = tmp_path / "bare-path"
    bare.mkdir(exist_ok=True)
    assert shutil.which("ffmpeg", path=str(bare)) is None
    return str(bare)


def _run_worker_in_child(audio: Path, env: dict[str, str]) -> dict[str, Any]:
    assert audio.is_file(), f"locked media fixture missing: {audio}"
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD_RUN_WORKER, str(audio)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    assert proc.returncode == 0 and lines, (
        f"child did not report (exit {proc.returncode}); stderr:\n{proc.stderr}"
    )
    return json.loads(lines[-1])


def _assert_no_implementation_detail(message: str) -> None:
    assert "FileNotFoundError" not in message
    assert "vocal_region_worker" not in message
    assert "exit " not in message
    assert "uv" not in message.lower().split()


def test_packaged_worker_uses_payload_runtime_and_script() -> None:
    code = (
        "import json; from pathlib import Path; from apps.vocals import cli; "
        "print(json.dumps(cli._worker_command(Path('/music/track.wav'))))"
    )
    env = _child_env(
        MDT_VOCAL_WORKER_PYTHON="/payload/runtime/bin/python3",
        MDT_VOCAL_WORKER_SCRIPT="/payload/app/scripts/vocal_region_worker.py",
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    assert json.loads(proc.stdout.splitlines()[-1]) == [
        "/payload/runtime/bin/python3",
        "/payload/app/scripts/vocal_region_worker.py",
        "--device",
        "auto",
        "/music/track.wav",
    ]


def test_missing_worker_runtime_has_plain_language_error(tmp_path: Path) -> None:
    missing_python = tmp_path / "payload" / "runtime" / "bin" / "python3"
    assert not missing_python.exists()
    env = _child_env(
        MDT_VOCAL_WORKER_PYTHON=str(missing_python),
        MDT_VOCAL_WORKER_SCRIPT=str(vocals_cli.WORKER_SCRIPT),
    )
    report = _run_worker_in_child(WAV_FIXTURE, env)

    assert report["type"] == "WorkerUnavailableError", report
    # The real OS launch failed; preflight did not pre-empt it.
    assert report["cause"] == "FileNotFoundError", report
    assert report["message"] == vocals_cli.WORKER_UNAVAILABLE_MESSAGE
    assert "reinstall open dj from a complete dmg" in report["message"].lower()
    _assert_no_implementation_detail(report["message"])


def test_non_executable_worker_runtime_has_plain_language_error(tmp_path: Path) -> None:
    """A runtime that is present but not executable (a damaged copy) is EACCES."""
    runtime = tmp_path / "payload" / "runtime" / "bin" / "python3"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(Path(sys.executable).resolve().read_bytes()[:4096])
    runtime.chmod(0o644)
    env = _child_env(
        MDT_VOCAL_WORKER_PYTHON=str(runtime),
        MDT_VOCAL_WORKER_SCRIPT=str(vocals_cli.WORKER_SCRIPT),
    )
    report = _run_worker_in_child(WAV_FIXTURE, env)

    assert report["type"] == "WorkerUnavailableError", report
    assert report["cause"] == "PermissionError", report
    assert report["message"] == vocals_cli.WORKER_UNAVAILABLE_MESSAGE


def test_missing_packaged_worker_script_gets_reinstall_guidance(tmp_path: Path) -> None:
    missing_script = tmp_path / "payload" / "app" / "scripts" / "vocal_region_worker.py"
    assert not missing_script.exists()
    env = _child_env(
        MDT_VOCAL_WORKER_PYTHON=sys.executable,
        MDT_VOCAL_WORKER_SCRIPT=str(missing_script),
    )
    report = _run_worker_in_child(WAV_FIXTURE, env)

    assert report["type"] == "WorkerUnavailableError", report
    assert report["cause"] is None, "refused before launch, not after a worker exit"
    assert report["message"] == vocals_cli.WORKER_UNAVAILABLE_MESSAGE
    _assert_no_implementation_detail(report["message"])


def test_present_packaged_worker_script_reaches_the_worker() -> None:
    """Control: the script guard must not refuse a script that is there."""
    env = _child_env(
        MDT_VOCAL_WORKER_PYTHON=sys.executable,
        MDT_VOCAL_WORKER_SCRIPT=str(vocals_cli.WORKER_SCRIPT),
    )
    report = _run_worker_in_child(WAV_FIXTURE, env)

    assert report["type"] != "WorkerUnavailableError", report


def test_non_wav_without_ffmpeg_refuses_before_launch(tmp_path: Path) -> None:
    env = _child_env(
        PATH=_empty_path_dir(tmp_path),
        MDT_VOCAL_WORKER_PYTHON=sys.executable,
        MDT_VOCAL_WORKER_SCRIPT=str(vocals_cli.WORKER_SCRIPT),
    )
    report = _run_worker_in_child(MP3_FIXTURE, env)

    assert report["type"] == "WorkerUnavailableError", report
    assert report["cause"] is None, "refused before launch, not after a worker exit"
    message = report["message"]
    assert MP3_FIXTURE.name in message
    assert "ffmpeg" in message
    assert vocals_cli.FFMPEG_OVERRIDE_ENV in message
    assert "convert the track to WAV" in message
    assert "vocal_region_worker" not in message


def test_wav_without_ffmpeg_still_reaches_the_worker(tmp_path: Path) -> None:
    """Control: WAV decodes via soundfile in the worker, so no ffmpeg needed."""
    env = _child_env(
        PATH=_empty_path_dir(tmp_path),
        MDT_VOCAL_WORKER_PYTHON=sys.executable,
        MDT_VOCAL_WORKER_SCRIPT=str(vocals_cli.WORKER_SCRIPT),
    )
    report = _run_worker_in_child(WAV_FIXTURE, env)

    assert report["type"] != "WorkerUnavailableError", report


def test_non_wav_with_mdt_ffmpeg_passes_the_decoder_preflight(tmp_path: Path) -> None:
    """Control: MDT_FFMPEG alone (PATH has none) satisfies the worker's lookup."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("UNAVAILABLE: no ffmpeg on this host to point MDT_FFMPEG at")
    bare_path = _empty_path_dir(tmp_path)
    environ = {"PATH": bare_path, vocals_cli.FFMPEG_OVERRIDE_ENV: ffmpeg}

    vocals_cli.preflight_worker(MP3_FIXTURE, environ)
    with pytest.raises(vocals_cli.WorkerUnavailableError):
        vocals_cli.preflight_worker(MP3_FIXTURE, {"PATH": bare_path})


def test_payload_launcher_exports_vocal_runtime_contract() -> None:
    from scripts.build_engine_payload import LAUNCHER_TEMPLATE

    assert "MDT_VOCAL_WORKER_PYTHON=\"$payload/runtime/bin/python3\"" in LAUNCHER_TEMPLATE
    assert (
        "MDT_VOCAL_WORKER_SCRIPT=\"$payload/app/scripts/vocal_region_worker.py\""
        in LAUNCHER_TEMPLATE
    )
