"""The odj-audio supervisor driving the real Rust binary (GSD plan 20-02).

Split from ``test_audio_engine.py``, whose stand-in engine covers each lifecycle
branch: these build ``odj-audio`` from this checkout and pin it, then connect to
its socket with the token the route publishes and load a real file on stdio.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

import pytest

from apps.engine_core.audio_engine import (
    BIN_ENV,
    AudioEngineError,
    AudioEngineSupervisor,
    resolve_binary,
)
from apps.engine_core.audio_engine_api import AUDIO_ENGINE_PATH
from apps.shared import platform_paths
from tests.engine_core.test_audio_engine import _app, _wait_for


# ----- the real engine -----------------------------------------------------------
def _bindgen_env(environ: Mapping[str, str]) -> dict[str, str]:
    """``environ`` with gcc's builtin headers added for bindgen, as ci.yml's cargo step does.

    signalsmith-stretch runs bindgen, and the CI runners' libclang ships without
    its resource headers ('stddef.h' file not found). Appended, so flags already
    set are kept; left alone where gcc has no include dir to give.
    """
    env = dict(environ)
    gcc = shutil.which("gcc")
    if gcc is None:
        return env
    include = subprocess.run(
        [gcc, "-print-file-name=include"], capture_output=True, text=True, check=False
    ).stdout.strip()
    if include and Path(include).is_absolute() and Path(include).is_dir():
        extra = env.get("BINDGEN_EXTRA_CLANG_ARGS", "")
        env["BINDGEN_EXTRA_CLANG_ARGS"] = f"{extra} -I{include}".strip()
    return env


@pytest.fixture(scope="module")
def real_engine_env() -> dict[str, str]:
    """The environment for a supervisor running odj-audio built from THIS checkout.

    Rebuilt every run (cargo does nothing when it is fresh) and pinned with
    ``ODJ_AUDIO_BIN``: without that the supervisor takes the newest binary under
    ``apps/audio-engine/target``, which on a self-hosted CI runner can be a
    leftover from another branch's job, so the test would exercise someone
    else's engine.
    """
    if shutil.which("cargo") is None:
        pytest.skip("UNAVAILABLE: no cargo here, so odj-audio cannot be built from this checkout")
    crate = platform_paths.PROJECT_ROOT / "apps" / "audio-engine"
    subprocess.run(
        [
            "cargo",
            "build",
            "--quiet",
            "--bin",
            "odj-audio",
            "--manifest-path",
            str(crate / "Cargo.toml"),
        ],
        check=True,
        env=_bindgen_env(os.environ),
    )
    built = (
        crate / "target" / "debug" / ("odj-audio.exe" if sys.platform == "win32" else "odj-audio")
    )
    env = {**os.environ, BIN_ENV: str(built)}
    assert resolve_binary(env, platform_paths.PROJECT_ROOT).path == built
    return env


def test_the_real_engine_takes_commands_over_the_published_socket(
    real_engine_env: dict[str, str],
) -> None:
    websockets = pytest.importorskip("websockets.sync.client")
    sup = AudioEngineSupervisor(environ=real_engine_env, repo_root=platform_paths.PROJECT_ROOT)
    try:
        client = _app(sup)
        assert client.post(f"{AUDIO_ENGINE_PATH}/start", json={"clock": "wall"}).status_code == 200
        _wait_for(lambda: client.get(AUDIO_ENGINE_PATH).json()["state"] == "running")
        body = client.get(AUDIO_ENGINE_PATH).json()
        # Without the token the engine refuses the socket.
        with pytest.raises(Exception, match="401"):
            websockets.connect(body["ws_url"], open_timeout=5)
        with websockets.connect(f"{body['ws_url']}?token={body['token']}", open_timeout=5) as ws:
            assert json.loads(ws.recv(timeout=5))["type"] == "hello"
            ws.send(json.dumps({"id": "x", "cmd": {"type": "crossfader", "value": 0.25}}))
            seen_result = seen_state = False
            deadline = time.monotonic() + 10
            while not (seen_result and seen_state) and time.monotonic() < deadline:
                msg = json.loads(ws.recv(timeout=5))
                if msg["type"] == "result" and msg["id"] == "x":
                    assert msg["ok"] is True
                    seen_result = True
                if msg["type"] == "state" and msg["mixer"]["crossfader"] == 0.25:
                    seen_state = True
            assert seen_result and seen_state
        pid = body["pid"]
        assert client.post(f"{AUDIO_ENGINE_PATH}/stop").json()["last_exit_code"] == 0
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        sup.stop()


def test_the_real_engine_loads_a_file_sent_on_stdio(
    tmp_path: Path, real_engine_env: dict[str, str]
) -> None:
    import math
    import struct
    import wave

    wav = tmp_path / "tone.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(48000)
        frames = b"".join(
            struct.pack("<hh", v, v)
            for v in (int(8000 * math.sin(2 * math.pi * 440 * i / 48000)) for i in range(48000))
        )
        w.writeframes(frames)
    sup = AudioEngineSupervisor(environ=real_engine_env, repo_root=platform_paths.PROJECT_ROOT)
    try:
        sup.start("wall")
        _wait_for(lambda: sup.status()["state"] == "running")
        grid = [{"n": (i % 4) + 1, "time_ms": i * 500.0} for i in range(2)]
        assert sup.command(
            {"type": "load", "deck": 1, "path": str(wav), "beatgrid": grid, "bpm": 120}
        )["ok"]
        with pytest.raises(AudioEngineError) as e:
            sup.command({"type": "load", "deck": 2, "path": str(tmp_path / "missing.wav")})
        assert e.value.code in ("io", "decode"), e.value.code
    finally:
        sup.stop()
