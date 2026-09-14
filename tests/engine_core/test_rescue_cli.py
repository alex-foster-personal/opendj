"""``python -m apps.engine_core rescue`` CLI parity (RESCUE-04)."""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest

from apps.engine_core.__main__ import main
from apps.engine_core.app import create_app
from apps.engine_core.config import EngineConfig, apply_env_contract, prepare_layout
from apps.engine_core.rescue.store import RescueStore
from tests.waits import start_uvicorn_in_thread

pytestmark = pytest.mark.requirement("RESCUE-04")


def _deck_snapshot(*, stable_id: str | None, playing: bool, deck_id: int) -> dict:
    return {
        "deck_id": deck_id,
        "stable_id": stable_id,
        "source_path": None,
        "playing": playing,
        "position_ms": 0,
        "beat_stamp": {"kind": "sample", "position_ms": 0},
        "pitch": 1.0,
        "pitch_range": 8,
        "master_tempo_enabled": True,
        "key_sync_enabled": False,
        "quantize_enabled": True,
        "beat_sync_enabled": True,
        "sync_mode": "bar",
        "is_master": False,
        "cue_ms": None,
        "loop": None,
        "hot_cue_armed": None,
        "stems": {
            "vocal": {"muted": False, "solo": False, "gain": 0.5},
            "instrumental": {"muted": False, "solo": False, "gain": 0.5},
            "drums": {"muted": False, "solo": False, "gain": 0.5},
        },
        "mixer_channel": {
            "trim": 0.5,
            "eq_high": 0.5,
            "eq_mid": 0.5,
            "eq_low": 0.5,
            "filter": 0.5,
            "fader": 1.0,
            "assign": "THRU",
            "cue_enabled": False,
        },
    }


@pytest.fixture
def engine_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "data"
    (data_dir / "state").mkdir(parents=True)
    monkeypatch.setenv("MDT_DATA_DIR", str(data_dir))
    monkeypatch.setenv("MDT_LIBRARY_MODE", "local")
    apply_env_contract(EngineConfig(data_dir=data_dir, host="127.0.0.1", port=8788))
    prepare_layout(EngineConfig(data_dir=data_dir, host="127.0.0.1", port=8788))

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = int(listener.getsockname()[1])
    monkeypatch.setenv("MUSIC_DJ_BACKEND_PORT", str(port))

    now_ms = int(time.time() * 1000)
    decks = {}
    for deck_id in (1, 2, 3, 4):
        deck = _deck_snapshot(
            stable_id="b" * 40 if deck_id == 1 else None,
            playing=deck_id == 1,
            deck_id=deck_id,
        )
        decks[deck_id] = deck
    payload = {
        "schema": 1,
        "captured_at_ms": now_ms - 5 * 60 * 1000,
        "reason": "periodic",
        "app_posture": "gig",
        "master_deck": None,
        "playlist_id": None,
        "deck_layout": "more",
        "decks": decks,
        "mixer": {
            "crossfader": 0.5,
            "master": 0.8,
            "headphones": {
                "mix": 0.5,
                "level": 0.5,
                "output_mode": "practice",
                "selected_master_output_device_id": None,
                "selected_output_device_id": None,
            },
        },
    }
    RescueStore(data_dir).append(
        captured_at_ms=payload["captured_at_ms"], payload=payload
    )

    app = create_app(EngineConfig(data_dir=data_dir, host="127.0.0.1", port=port))
    server, thread = start_uvicorn_in_thread(
        __import__("uvicorn").Config(app, log_level="warning"),
        what="rescue CLI daemon",
        sockets=[listener],
    )
    try:
        yield data_dir, port
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)


def test_rescue_list_json(engine_daemon, capsys: pytest.CaptureFixture[str]) -> None:
    data_dir, port = engine_daemon
    capsys.readouterr()
    assert (
        main(
            [
                "rescue",
                "list",
                "--data-dir",
                str(data_dir),
                "--port",
                str(port),
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["snapshots"]) == 1


def test_rescue_restore_play_json(engine_daemon, capsys: pytest.CaptureFixture[str]) -> None:
    data_dir, port = engine_daemon
    capsys.readouterr()
    assert (
        main(
            [
                "rescue",
                "restore",
                "--data-dir",
                str(data_dir),
                "--port",
                str(port),
                "--play",
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "play"
    assert payload["decks"]["1"]["outcome"] == "resumed"
