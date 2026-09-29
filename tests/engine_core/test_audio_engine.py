"""The odj-audio supervisor and its origin route (GSD plan 20-02).

Most tests drive the supervisor against a stand-in engine: a short script that
speaks the start of protocol v1 (the stdout ``hello``) and then misbehaves in
one chosen way, so each lifecycle branch is exercised through a real child
process, real pipes and real exit codes. The last test runs the real Rust
binary and connects to its socket with the token the route publishes.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.engine_core.audio_engine import (
    AUTOSTART_ENV,
    BIN_ENV,
    TOKEN_ENV,
    AudioEngineError,
    AudioEngineSupervisor,
    autostart_from_environ,
    resolve_binary,
)
from apps.engine_core.audio_engine_api import AUDIO_ENGINE_PATH, add_audio_engine_routes
from apps.shared import platform_paths

FAKE_ENGINE = f"""#!{sys.executable}
import json, os, sys, time
mode = os.environ["FAKE_MODE"]
log = os.environ["FAKE_LOG"]
with open(log, "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "token": os.environ.get("{TOKEN_ENV}")}}) + "\\n")
if mode == "nohello":
    sys.exit(1)
proto = 2 if mode == "proto2" else 1
hello = {{"type": "hello", "protocol": proto, "engine": "fake", "clock": sys.argv[3],
          "sample_rate": 48000, "ws": "ws://127.0.0.1:9/api/v1/ws"}}
print(json.dumps(hello), flush=True)
print(json.dumps({{"type": "state", "frame": 0}}), flush=True)
if mode == "crash":
    time.sleep(0.05)
    sys.exit(3)
if mode == "ignore_eof":
    sys.stdin.read()
    time.sleep(60)
for line in sys.stdin:
    msg = json.loads(line)
    with open(log, "a") as f:
        f.write(json.dumps({{"cmd": msg["cmd"]}}) + "\\n")
    if msg["cmd"]["type"] == "exit":
        sys.exit(5)
    if msg["cmd"]["type"] == "hang":
        continue
    ok = msg["cmd"]["type"] != "refuse"
    res = {{"type": "result", "id": msg["id"], "ok": ok}}
    if not ok:
        res["error"] = {{"code": "decode", "message": "no such audio"}}
    print(json.dumps({{"type": "state", "frame": 1, "note": "result"}}), flush=True)
    print(json.dumps(res), flush=True)
sys.exit(0)
"""


def _fake_engine(tmp_path: Path) -> Path:
    p = tmp_path / "odj-audio"
    p.write_text(FAKE_ENGINE, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


def _spawns(log: Path) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    return [r for r in rows if "argv" in r]


def _commands(log: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    return [r["cmd"] for r in rows if "cmd" in r]


def _wait_for(pred: Callable[[], bool], timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


@pytest.fixture
def make_supervisor(tmp_path: Path) -> Iterator[Callable[..., AudioEngineSupervisor]]:
    made: list[AudioEngineSupervisor] = []
    log = tmp_path / "spawns.jsonl"

    def make(mode: str, **kw: Any) -> AudioEngineSupervisor:
        environ = {
            **os.environ,
            BIN_ENV: str(_fake_engine(tmp_path)),
            "FAKE_MODE": mode,
            "FAKE_LOG": str(log),
        }
        kw.setdefault("backoff_s", (0.01,))
        kw.setdefault("stop_timeout_s", 1.0)
        sup = AudioEngineSupervisor(environ=environ, repo_root=tmp_path, **kw)
        made.append(sup)
        return sup

    yield make
    for sup in made:
        sup.stop()


# ----- binary resolution -----------------------------------------------------
def test_env_binary_wins_and_is_never_second_guessed(tmp_path: Path) -> None:
    # A repo build exists, but ODJ_AUDIO_BIN points somewhere wrong: the answer
    # is the refusal, never the repo build (a packaged app must not run one).
    repo_bin = tmp_path / "apps/audio-engine/target/release/odj-audio"
    repo_bin.parent.mkdir(parents=True)
    repo_bin.write_text("#!/bin/sh\n")
    repo_bin.chmod(0o755)
    with pytest.raises(AudioEngineError, match="is not a file") as e:
        resolve_binary({BIN_ENV: str(tmp_path / "missing")}, tmp_path)
    assert e.value.code == "unavailable"
    not_exec = tmp_path / "plain"
    not_exec.write_text("x")
    not_exec.chmod(0o644)
    with pytest.raises(AudioEngineError, match="not executable"):
        resolve_binary({BIN_ENV: str(not_exec)}, tmp_path)
    # Control: with no env, the same repo build is found.
    assert resolve_binary({}, tmp_path).source == "repo-release"


def test_newest_repo_build_is_used(tmp_path: Path) -> None:
    for profile, mtime in (("release", 1_000), ("debug", 2_000)):
        p = tmp_path / f"apps/audio-engine/target/{profile}/odj-audio"
        p.parent.mkdir(parents=True)
        p.write_text("#!/bin/sh\n")
        p.chmod(0o755)
        os.utime(p, (mtime, mtime))
    assert resolve_binary({}, tmp_path).source == "repo-debug"
    with pytest.raises(AudioEngineError, match="cargo build"):
        resolve_binary({}, tmp_path / "elsewhere")


def test_autostart_is_a_fail_fast_enum() -> None:
    assert autostart_from_environ({}) == "off"
    assert autostart_from_environ({AUTOSTART_ENV: "Device"}) == "device"
    with pytest.raises(ValueError, match=AUTOSTART_ENV):
        autostart_from_environ({AUTOSTART_ENV: "on"})


# ----- lifecycle ---------------------------------------------------------------
def test_start_publishes_the_socket_and_stop_is_not_a_crash(
    make_supervisor: Callable[..., AudioEngineSupervisor], tmp_path: Path
) -> None:
    sup = make_supervisor("ok")
    sup.start("wall")
    _wait_for(lambda: sup.status()["state"] == "running")
    st = sup.status()
    assert st["ws_url"] == "ws://127.0.0.1:9/api/v1/ws"
    assert st["protocol"] == 1 and st["clock"] == "wall" and st["generation"] == 1
    assert st["binary_source"] == "env"
    spawn = _spawns(tmp_path / "spawns.jsonl")[0]
    assert spawn["argv"] == ["serve", "--clock", "wall", "--ws", "127.0.0.1:0"]
    # The token the engine was given is the one published, and it is long.
    assert spawn["token"] == st["token"] and len(st["token"]) >= 32
    # Same clock again changes nothing; another clock is refused.
    assert sup.start("wall")["generation"] == 1
    with pytest.raises(AudioEngineError) as e:
        sup.start("device")
    assert e.value.code == "conflict"

    st = sup.stop()
    assert st["state"] == "stopped"
    assert st["last_exit_code"] == 0, "the engine exited on stdin EOF"
    assert st["restarts"] == 0
    assert st["token"] is None and st["ws_url"] is None


def test_a_crash_restarts_with_a_new_token_until_the_limit(
    make_supervisor: Callable[..., AudioEngineSupervisor], tmp_path: Path
) -> None:
    sup = make_supervisor("crash", crash_limit=3)
    sup.start("wall")
    _wait_for(lambda: sup.status()["state"] == "failed")
    st = sup.status()
    assert st["generation"] == 3 and st["restarts"] == 2
    assert st["last_exit_code"] == 3
    assert "3 times" in st["error"]
    tokens = [s["token"] for s in _spawns(tmp_path / "spawns.jsonl")]
    assert len(tokens) == 3 and len(set(tokens)) == 3


def test_a_wrong_protocol_is_failed_at_once(
    make_supervisor: Callable[..., AudioEngineSupervisor], tmp_path: Path
) -> None:
    sup = make_supervisor("proto2")
    sup.start("wall")
    _wait_for(lambda: sup.status()["state"] == "failed")
    assert "protocol 2" in sup.status()["error"]
    time.sleep(0.2)
    assert len(_spawns(tmp_path / "spawns.jsonl")) == 1, "a wrong protocol is not retried"


def test_an_engine_that_ignores_eof_is_terminated(
    make_supervisor: Callable[..., AudioEngineSupervisor],
) -> None:
    sup = make_supervisor("ignore_eof", stop_timeout_s=0.3)
    sup.start("wall")
    _wait_for(lambda: sup.status()["state"] == "running")
    pid = sup.status()["pid"]
    t0 = time.monotonic()
    st = sup.stop()
    assert time.monotonic() - t0 < 5
    assert st["state"] == "stopped"
    assert st["last_exit_code"] is not None and st["last_exit_code"] != 0
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_missing_binary_is_unavailable_not_a_crash(tmp_path: Path) -> None:
    sup = AudioEngineSupervisor(environ={BIN_ENV: str(tmp_path / "nope")}, repo_root=tmp_path)
    with pytest.raises(AudioEngineError):
        sup.start("wall")
    st = sup.status()
    assert st["state"] == "unavailable" and "nope" in st["error"]
    # autostart reports rather than raises: the Python engine must still boot.
    sup = AudioEngineSupervisor(
        environ={BIN_ENV: str(tmp_path / "nope"), AUTOSTART_ENV: "wall"}, repo_root=tmp_path
    )
    sup.autostart()
    assert sup.status()["state"] == "unavailable"


# ----- the route ---------------------------------------------------------------
def _app(sup: AudioEngineSupervisor) -> TestClient:
    app = FastAPI()
    add_audio_engine_routes(app, sup)
    return TestClient(app)


def test_route_starts_publishes_and_stops(
    make_supervisor: Callable[..., AudioEngineSupervisor],
) -> None:
    client = _app(make_supervisor("ok"))
    assert client.get(AUDIO_ENGINE_PATH).json()["state"] == "off"
    r = client.post(f"{AUDIO_ENGINE_PATH}/start", json={"clock": "wall"})
    assert r.status_code == 200, r.text
    _wait_for(lambda: client.get(AUDIO_ENGINE_PATH).json()["state"] == "running")
    body = client.get(AUDIO_ENGINE_PATH).json()
    assert body["ws_url"].startswith("ws://127.0.0.1:") and body["token"]
    r = client.post(f"{AUDIO_ENGINE_PATH}/start", json={"clock": "device"})
    assert r.status_code == 409 and r.json()["error"] == "audio_engine_conflict"
    assert client.post(f"{AUDIO_ENGINE_PATH}/start", json={"clock": "fake"}).status_code == 422
    assert client.post(f"{AUDIO_ENGINE_PATH}/stop").json()["state"] == "stopped"


def test_route_503_names_the_missing_binary(tmp_path: Path) -> None:
    sup = AudioEngineSupervisor(environ={BIN_ENV: str(tmp_path / "nope")}, repo_root=tmp_path)
    r = _app(sup).post(f"{AUDIO_ENGINE_PATH}/start", json={"clock": "wall"})
    assert r.status_code == 503
    assert r.json()["error"] == "audio_engine_unavailable"
    assert "nope" in r.json()["message"]


# ----- the real engine -----------------------------------------------------------
def _real_binary() -> Path | None:
    try:
        return resolve_binary(os.environ, platform_paths.PROJECT_ROOT).path
    except AudioEngineError:
        return None


@pytest.mark.skipif(
    _real_binary() is None,
    reason="no odj-audio build; cargo build --manifest-path apps/audio-engine/Cargo.toml",
)
def test_the_real_engine_takes_commands_over_the_published_socket() -> None:
    websockets = pytest.importorskip("websockets.sync.client")
    sup = AudioEngineSupervisor(environ=dict(os.environ), repo_root=platform_paths.PROJECT_ROOT)
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


# ----- the real engine app ------------------------------------------------------
# In a subprocess, like test_data_dir_sandbox: create_app imports the legacy
# modules, which resolve their paths from MDT_DATA_DIR at import time.
_BOOT_PROBE = """
import json, os
from pathlib import Path
from starlette.testclient import TestClient
from apps.engine_core.app import create_app
from apps.engine_core.config import EngineBootError, EngineConfig

try:
    app = create_app(EngineConfig(data_dir=Path(os.environ["MDT_DATA_DIR"])))
except EngineBootError as exc:
    print(json.dumps({"boot_error": str(exc)}))
    raise SystemExit(0)
with TestClient(app, base_url="http://127.0.0.1") as client:
    print(json.dumps({"status": client.get("/api/v1/audio-engine").json()}))
"""


def _boot(tmp_path: Path, autostart: str | None) -> dict[str, Any]:
    import subprocess

    env = {
        **os.environ,
        "PYTHONPATH": str(platform_paths.PROJECT_ROOT),
        "MDT_DATA_DIR": str(tmp_path / "data"),
        "MDT_LIBRARY_MODE": "local",
        "HOME": str(tmp_path),
        BIN_ENV: str(tmp_path / "no-such-engine"),
    }
    env.pop("WEB_CONCURRENCY", None)
    env.pop(AUTOSTART_ENV, None)
    if autostart is not None:
        env[AUTOSTART_ENV] = autostart
    r = subprocess.run(
        [sys.executable, "-c", _BOOT_PROBE],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(platform_paths.PROJECT_ROOT),
        timeout=180,
        check=False,
    )
    assert r.returncode == 0, r.stderr[-4000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_the_engine_app_mounts_the_route_and_refuses_a_bad_autostart(tmp_path: Path) -> None:
    out = _boot(tmp_path / "a", "on")
    assert AUTOSTART_ENV in out["boot_error"]
    # Control: default off boots, and the route is there on the real app.
    assert _boot(tmp_path / "b", None)["status"]["state"] == "off"
    # Autostart with a missing binary still boots, and says why it is not up.
    st = _boot(tmp_path / "c", "wall")["status"]
    assert st["state"] == "unavailable" and "no-such-engine" in st["error"]


# ----- commands on stdio, and load by stable_id ---------------------------------
def test_command_waits_for_its_own_result(
    make_supervisor: Callable[..., AudioEngineSupervisor], tmp_path: Path
) -> None:
    sup = make_supervisor("ok")
    with pytest.raises(AudioEngineError) as e:
        sup.command({"type": "play", "deck": 1, "playing": True})
    assert e.value.code == "not_running"
    sup.start("wall")
    _wait_for(lambda: sup.status()["state"] == "running")
    # A state line that merely mentions "result" is not taken for one.
    assert sup.command({"type": "play", "deck": 1, "playing": True})["ok"] is True
    with pytest.raises(AudioEngineError) as e:
        sup.command({"type": "refuse"})
    assert e.value.code == "decode" and "no such audio" in str(e.value)
    with pytest.raises(AudioEngineError) as e:
        sup.command({"type": "hang"}, timeout_s=0.3)
    assert e.value.code == "timeout"
    assert [c["type"] for c in _commands(tmp_path / "spawns.jsonl")] == ["play", "refuse", "hang"]


def test_a_command_in_flight_fails_when_the_engine_exits(
    make_supervisor: Callable[..., AudioEngineSupervisor],
) -> None:
    sup = make_supervisor("ok", crash_limit=1)
    sup.start("wall")
    _wait_for(lambda: sup.status()["state"] == "running")
    t0 = time.monotonic()
    with pytest.raises(AudioEngineError) as e:
        sup.command({"type": "exit"}, timeout_s=10)
    assert e.value.code == "not_running"
    assert time.monotonic() - t0 < 5, "the waiter is released at exit, not at its timeout"


def test_grid_from_anlz_converts_seconds_to_ms_and_names_a_missing_grid() -> None:
    from apps.engine_core.audio_engine_load import grid_from_anlz, load_command

    own = {
        "source": "own",
        "status": "ok",
        "bpm": 124.0,
        "beat_count": 3,
        "beats": [{"n": 4, "bpm": 124.0, "t": 0.1}, {"n": 1, "bpm": 124.0, "t": 0.5838}],
    }
    g = grid_from_anlz(own)
    assert g.source == "own" and g.bpm == 124.0 and g.missing_reason is None
    assert g.beats == [{"n": 4, "time_ms": 100.0}, {"n": 1, "time_ms": pytest.approx(583.8)}]
    cmd = load_command(2, "/music/a.mp3", g)
    assert cmd["beatgrid"] == g.beats and cmd["bpm"] == 124.0 and cmd["deck"] == 2
    # rekordbox grids carry bpm per beat only.
    rb = {"source": "rekordbox", "beat_count": 1, "beats": [{"n": 1, "bpm": 128.0, "t": 0.0}]}
    assert grid_from_anlz(rb).bpm == 128.0
    missing = grid_from_anlz(
        {"source": "own", "status": "missing", "beats": [], "reason": "not analyzed"}
    )
    assert missing.beats == [] and missing.missing_reason == "not analyzed"
    assert "beatgrid" not in load_command(1, "/a", missing)
    with pytest.raises(ValueError, match="do not increase"):
        grid_from_anlz(
            {"source": "own", "beats": [{"n": 1, "bpm": 1, "t": 1}, {"n": 2, "bpm": 1, "t": 1}]}
        )


def test_load_route_sends_the_file_and_the_grid_the_page_sees(
    make_supervisor: Callable[..., AudioEngineSupervisor],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException
    from fastapi.responses import JSONResponse

    from apps.engine_core import audio_engine_api
    from apps.webui.server.deps import get_read_state

    class Picked:
        path = tmp_path / "track.flac"

    seen: dict[str, Any] = {}

    def resolve(stable_id: str, **kw: Any) -> Picked:
        seen["resolve"] = (stable_id, kw["share"])
        if stable_id == "gone":
            raise HTTPException(404, {"code": "AUDIO_NOT_FOUND"})
        return Picked()

    def anlz(request: Any, stable_id: str, points: int, backend: Any) -> JSONResponse:
        if stable_id == "nogrid":
            raise HTTPException(404, {"code": "ANALYSIS_NOT_FOUND"})
        return JSONResponse(
            {
                "beatgrid": {
                    "source": "own",
                    "bpm": 120.0,
                    "beats": [{"n": 1, "bpm": 120.0, "t": 0.25}],
                }
            }
        )

    monkeypatch.setattr(audio_engine_api.rb_vendor, "resolve_playable_audio", resolve)
    monkeypatch.setattr(audio_engine_api, "get_track_anlz", anlz)
    sup = make_supervisor("ok")
    app = FastAPI()
    add_audio_engine_routes(app, sup)
    app.dependency_overrides[get_read_state] = lambda: None
    client = TestClient(app)

    r = client.post(f"{AUDIO_ENGINE_PATH}/load", json={"deck": 1, "stable_id": "abc"})
    assert r.status_code == 409 and r.json()["error"] == "audio_engine_not_running"
    sup.start("wall")
    _wait_for(lambda: sup.status()["state"] == "running")

    r = client.post(f"{AUDIO_ENGINE_PATH}/load", json={"deck": 2, "stable_id": "abc"})
    assert r.status_code == 200, r.text
    assert r.json() == {
        "deck": 2,
        "stable_id": "abc",
        "path": str(Picked.path),
        "beatgrid_source": "own",
        "beats": 1,
        "bpm": 120.0,
        "beatgrid_missing": None,
    }
    assert seen["resolve"] == ("abc", False)
    assert _commands(tmp_path / "spawns.jsonl")[-1] == {
        "type": "load",
        "deck": 2,
        "path": str(Picked.path),
        "beatgrid": [{"n": 1, "time_ms": 250.0}],
        "bpm": 120.0,
    }

    # No analysis: it still loads, and says why there is no grid.
    r = client.post(f"{AUDIO_ENGINE_PATH}/load", json={"deck": 1, "stable_id": "nogrid"})
    assert r.status_code == 200 and "ANALYSIS_NOT_FOUND" in r.json()["beatgrid_missing"]
    assert "beatgrid" not in _commands(tmp_path / "spawns.jsonl")[-1]
    # No file: the library's own 404 passes through; nothing is sent.
    n = len(_commands(tmp_path / "spawns.jsonl"))
    assert (
        client.post(f"{AUDIO_ENGINE_PATH}/load", json={"deck": 1, "stable_id": "gone"}).status_code
        == 404
    )
    assert len(_commands(tmp_path / "spawns.jsonl")) == n
    assert (
        client.post(f"{AUDIO_ENGINE_PATH}/load", json={"deck": 5, "stable_id": "abc"}).status_code
        == 422
    )


@pytest.mark.skipif(
    _real_binary() is None,
    reason="no odj-audio build; cargo build --manifest-path apps/audio-engine/Cargo.toml",
)
def test_the_real_engine_loads_a_file_sent_on_stdio(tmp_path: Path) -> None:
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
    sup = AudioEngineSupervisor(environ=dict(os.environ), repo_root=platform_paths.PROJECT_ROOT)
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
