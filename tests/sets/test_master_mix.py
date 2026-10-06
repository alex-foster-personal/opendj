"""REC "Master mix (internal)": the page streams its master bus, the daemon writes WAV (SET-12).

Mutation controls (run by hand on demon-llama for this change, each went red):
dropping the seq check in MasterMixWriter.append, dropping the header rewrite in
_Segment.append, and defaulting RecorderStartRequest.source to "master".
"""
from __future__ import annotations

import json
import math
import os
import struct
import subprocess
import sys
import wave
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.sets import capture, master_mix, rec_cli
from apps.sets.__main__ import main as sets_main
from apps.sets.api import router
from apps.sets.recorder_service import RecorderService

pytestmark = pytest.mark.requirement("SET-12")

REPO_ROOT = Path(__file__).resolve().parents[2]
RATE = 48_000
SESSION = "2026-10-06T03-00-00"


def _tone(frames: int, amplitude: float = 0.5, freq: float = 440.0) -> bytes:
    out = bytearray()
    for i in range(frames):
        v = int(amplitude * 32767 * math.sin(2 * math.pi * freq * i / RATE))
        out += struct.pack("<hh", v, v)
    return bytes(out)


class _Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def _writer(tmp_path: Path, clock: _Clock | None = None, **kw) -> master_mix.MasterMixWriter:
    return master_mix.MasterMixWriter(
        tmp_path,
        monotonic=clock or _Clock(),
        utc_now=lambda: datetime(2026, 10, 6, 3, 0, 0, tzinfo=UTC),
        **kw,
    )


def _wav(path: Path) -> tuple[int, int, int]:
    with wave.open(str(path), "rb") as fh:
        return fh.getnchannels(), fh.getframerate(), fh.getnframes()


def test_chunks_become_one_playable_stereo_wav(tmp_path: Path) -> None:
    """[if] chunks 0,1,2 arrive [then] one WAV holds every frame, [else stop]."""
    writer = _writer(tmp_path)
    for seq in range(3):
        writer.append(stream="s1", seq=seq, sample_rate=RATE, pcm=_tone(24_000))
    segment = tmp_path / "audio_2026-10-06T03-00-00.wav"
    # Readable BEFORE close: the header is rewritten per chunk, so a killed
    # daemon leaves a playable file.
    assert _wav(segment) == (2, RATE, 72_000)
    writer.close()
    assert _wav(segment) == (2, RATE, 72_000)
    assert writer.current_state() == "stopped"


def test_a_lost_chunk_is_refused_not_written_around(tmp_path: Path) -> None:
    """[if] seq skips [then] the chunk is refused and nothing is written, [else stop]."""
    writer = _writer(tmp_path)
    writer.append(stream="s1", seq=0, sample_rate=RATE, pcm=_tone(480))
    with pytest.raises(master_mix.MasterMixChunkRefused, match="1 was expected"):
        writer.append(stream="s1", seq=2, sample_rate=RATE, pcm=_tone(480))
    with pytest.raises(master_mix.MasterMixChunkRefused, match="must start at seq 0"):
        writer.append(stream="s2", seq=5, sample_rate=RATE, pcm=_tone(480))
    writer.close()
    assert _wav(tmp_path / "audio_2026-10-06T03-00-00.wav")[2] == 480


@pytest.mark.parametrize("pcm", [b"", b"\x00\x01\x02", b"\x00" * 6])
def test_a_chunk_that_is_not_whole_stereo_frames_is_refused(tmp_path: Path, pcm: bytes) -> None:
    """[if] a chunk is empty or splits a frame [then] it is refused, [else stop]."""
    with pytest.raises(master_mix.MasterMixChunkRefused):
        _writer(tmp_path).append(stream="s", seq=0, sample_rate=RATE, pcm=pcm)


def test_a_new_tap_or_a_new_rate_starts_a_new_segment(tmp_path: Path) -> None:
    """[if] the stream or rate changes [then] a new segment opens, never a splice, [else stop]."""
    writer = _writer(tmp_path)
    writer.append(stream="a", seq=0, sample_rate=RATE, pcm=_tone(480))
    writer.append(stream="b", seq=0, sample_rate=RATE, pcm=_tone(480))
    writer.append(stream="b", seq=1, sample_rate=44_100, pcm=_tone(441))
    writer.close()
    segments = sorted(tmp_path.glob("audio_*.wav"))
    assert [p.name for p in segments] == [
        "audio_2026-10-06T03-00-00.wav",
        "audio_2026-10-06T03-00-01.wav",
        "audio_2026-10-06T03-00-02.wav",
    ]
    assert [_wav(p)[1:] for p in segments] == [(RATE, 480), (RATE, 480), (44_100, 441)]


def test_segments_roll_at_the_segment_length(tmp_path: Path) -> None:
    """[if] a segment reaches its length [then] the next chunk opens a new file, [else stop]."""
    writer = _writer(tmp_path, segment_seconds=1)
    for seq in range(3):
        writer.append(stream="s", seq=seq, sample_rate=RATE, pcm=_tone(RATE // 2))
    writer.close()
    assert [_wav(p)[2] for p in sorted(tmp_path.glob("audio_*.wav"))] == [RATE, RATE // 2]


def test_capture_state_tracks_the_page_feeding_it(tmp_path: Path) -> None:
    """[if] chunks stop arriving [then] the capture reads failed with why, [else stop]."""
    clock = _Clock()
    writer = _writer(tmp_path, clock)
    assert writer.current_state() == "starting"
    writer.append(stream="s", seq=0, sample_rate=RATE, pcm=_tone(480))
    clock.t += master_mix.STALL_TIMEOUT_S - 1
    assert (writer.current_state(), writer.error) == ("recording", None)
    clock.t += 2
    assert writer.current_state() == "failed"
    assert "closed or its audio engine stopped" in (writer.error or "")

    never_fed = _writer(tmp_path / "other", clock)
    clock.t += master_mix.ATTACH_TIMEOUT_S + 1
    assert never_fed.current_state() == "failed"
    assert "open /performance page" in (never_fed.error or "")


@pytest.fixture
def master_client(tmp_path: Path):
    service = RecorderService(
        sets_root=tmp_path / "sets",
        db_path=tmp_path / "sets" / "sets.db",
        capture_enabled=False,
        list_devices=lambda: [
            capture.InputDevice(0, "MacBook Pro Microphone", False),
            capture.InputDevice(1, "BlackHole 2ch", True),
        ],
    )
    app = FastAPI()
    app.state.sets_recorder_service = service
    app.include_router(router)
    with TestClient(app) as client:
        yield client, service


def _pcm(client: TestClient, seq: int, pcm: bytes, session: str = SESSION, stream: str = "tab-1"):
    return client.post(
        f"/api/sets/recorder/{session}/master-pcm",
        params={"stream": stream, "seq": seq, "sample_rate": RATE},
        content=pcm,
        headers={"content-type": "application/octet-stream"},
    )


def test_source_master_records_the_streamed_mix_into_the_session(master_client) -> None:
    """[if] REC starts source=master and chunks arrive [then] the set holds that WAV, [else stop]."""
    client, service = master_client
    started = client.post(
        "/api/sets/recorder/start",
        json={"session_id": SESSION, "source": "master", "sources": []},
    )
    assert started.status_code == 201, started.text
    assert started.json()["capture"] == "starting"
    assert started.json()["capture_source"] == "master"
    assert started.json()["recordings_dir"] == str(service.sets_root)
    for seq in range(4):
        assert _pcm(client, seq, _tone(24_000)).status_code == 204
    status = client.get("/api/sets/recorder").json()
    assert (status["capture"], status["capture_source"]) == ("recording", "master")
    client.post(f"/api/sets/recorder/{SESSION}/stop")
    manifest = json.loads((service.sets_root / SESSION / "manifest.json").read_text())
    assert manifest["capture_device"] == master_mix.MASTER_MIX_DEVICE_LABEL
    [segment] = manifest["mp3_segments"]
    assert _wav(service.sets_root / SESSION / segment["name"]) == (2, RATE, 96_000)
    assert service.remembered_input() == {"kind": "master"}


def test_master_pcm_is_409_when_it_cannot_land(master_client) -> None:
    """[if] a chunk has no master recording or leaves a hole [then] 409, [else stop]."""
    client, _ = master_client
    assert _pcm(client, 0, _tone(480)).status_code == 409
    client.post(
        "/api/sets/recorder/start",
        json={"session_id": SESSION, "source": "loopback", "device_name": "BlackHole 2ch", "sources": []},
    )
    refused = _pcm(client, 0, _tone(480))
    assert refused.status_code == 409
    assert "not the master mix" in refused.json()["detail"]
    client.post(f"/api/sets/recorder/{SESSION}/stop")

    client.post("/api/sets/recorder/start", json={"session_id": SESSION + "_1", "source": "master", "sources": []})
    assert _pcm(client, 0, _tone(480), session=SESSION + "_1").status_code == 204
    gap = _pcm(client, 2, _tone(480), session=SESSION + "_1")
    assert gap.status_code == 409
    assert "a chunk was lost" in gap.json()["detail"]
    assert _pcm(client, 1, b"\x00\x01\x02", session=SESSION + "_1").status_code == 409


def test_master_pcm_requires_its_stream_seq_and_rate(master_client) -> None:
    """[if] a chunk omits stream, seq or rate [then] 422, never a guess, [else stop]."""
    client, _ = master_client
    response = client.post(
        f"/api/sets/recorder/{SESSION}/master-pcm", params={"seq": 0}, content=_tone(480)
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("source", "device"),
    [("loopback", "MacBook Pro Microphone"), ("external", "BlackHole 2ch")],
)
def test_a_source_that_contradicts_its_input_is_422(master_client, source: str, device: str) -> None:
    """[if] loopback names a mic or external a loopback [then] 422, nothing starts, [else stop]."""
    client, _ = master_client
    response = client.post(
        "/api/sets/recorder/start", json={"source": source, "device_name": device, "sources": []}
    )
    assert response.status_code == 422
    assert "loopback input" in response.json()["detail"]
    assert client.get("/api/sets/recorder").json()["active"] is False


def test_rec_cli_sends_the_same_explicit_source_as_the_picker(monkeypatch) -> None:
    """[if] `rec start --source master` runs [then] it posts source master, [else stop]."""
    calls: list[tuple[str, str, object]] = []

    def fake_call(base_url: str, method: str, path: str, body=None):
        calls.append((method, path, body))
        return {"active": True, "capture_source": "master"}

    monkeypatch.setattr(rec_cli, "_call", fake_call)
    assert sets_main(["rec", "start", "--base-url", "http://127.0.0.1:1", "--source", "master"]) == 0
    assert calls == [
        (
            "POST",
            "/api/sets/recorder/start",
            {"session_id": None, "source": "master", "sources": ["djay_monitor", "opendj_decks"]},
        )
    ]
    with pytest.raises(SystemExit):
        sets_main(["rec", "start", "--base-url", "http://127.0.0.1:1"])
    with pytest.raises(rec_cli.RecCommandFailed, match="needs --device-name"):
        rec_cli.start_body("loopback", None, [])
    with pytest.raises(rec_cli.RecCommandFailed, match="takes no --device-name"):
        rec_cli.start_body("master", "BlackHole 2ch", [])


_ENGINE_PROBE = """
import json, math, os, struct
from pathlib import Path
from starlette.testclient import TestClient
from apps.engine_core.app import create_app
from apps.engine_core.config import EngineConfig

app = create_app(EngineConfig(data_dir=Path(os.environ["MDT_DATA_DIR"])))
pcm = b"".join(struct.pack("<hh", v, v) for v in (int(9000 * math.sin(i / 7)) for i in range(48000)))
with TestClient(app, base_url="http://127.0.0.1") as client:
    start = client.post("/api/sets/recorder/start", json={"source": "master", "sources": []})
    sid = start.json().get("session_id")
    chunk = client.post(
        f"/api/sets/recorder/{sid}/master-pcm",
        params={"stream": "probe", "seq": 0, "sample_rate": 48000},
        content=pcm,
        headers={"content-type": "application/octet-stream"},
    )
    status = client.get("/api/sets/recorder").json()
    stop = client.post(f"/api/sets/recorder/{sid}/stop")
print(json.dumps({"start": start.status_code, "chunk": chunk.status_code,
                  "capture": status.get("capture"), "stop": stop.status_code}))
"""


def test_the_packaged_engine_app_records_the_master_mix(tmp_path: Path) -> None:
    """[if] engine_core serves REC source=master [then] start, chunk, stop all succeed, [else stop]."""
    result = subprocess.run(
        [sys.executable, "-c", _ENGINE_PROBE],
        cwd=REPO_ROOT,
        env={**os.environ, "MDT_DATA_DIR": str(tmp_path / "engine-data")},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out == {"start": 201, "chunk": 204, "capture": "recording", "stop": 200}
