"""REC "Master mix (internal)": the page streams its master bus, the daemon writes WAV (SET-12).

[if] the page streams its master mix [then] REC writes it as a WAV set, [else stop].

Mutation controls (run by hand on the Air for this change, each went red):
dropping the seq check in MasterMixWriter.append; dropping the header rewrite in
_Segment.append; writing one chunk twice in MasterMixWriter.append (caught by the
exact frame-count tests); defaulting RecorderStartRequest.source to "master".
"""
from __future__ import annotations

import asyncio
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
from apps.sets.master_tap import MASTER_TAP_CHANNEL_STATE, MasterTapUnavailable
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



def _wav_frames_total(directory: Path) -> int:
    return sum(_wav(p)[2] for p in sorted(directory.glob("audio_*.wav")))


# Irregular sizes on purpose: a pad, a dropped tail or a repeated chunk cannot
# hide inside whole-second arithmetic.
_CHUNK_FRAMES = [24_000, 24_000, 17_311, 24_000, 3, 24_000, 9_999, 24_000]


def test_wav_frames_equal_frames_sent_exactly_across_rolls_and_streams(tmp_path: Path) -> None:
    """[if] chunks roll segments and change stream [then] WAV frames == frames sent, [else stop]."""
    writer = _writer(tmp_path, segment_seconds=1)
    sent = 0
    for seq, frames in enumerate(_CHUNK_FRAMES):
        writer.append(stream="a", seq=seq, sample_rate=RATE, pcm=_tone(frames))
        sent += frames
    for seq, frames in enumerate([480, 7]):
        writer.append(stream="b", seq=seq, sample_rate=RATE, pcm=_tone(frames))
        sent += frames
    writer.close()
    assert len(list(tmp_path.glob("audio_*.wav"))) >= 4, "the roll and the stream change must both open segments"
    summary = writer.summary()
    assert {k: summary[k] for k in ("frames_accepted", "chunks_accepted", "streams")} == {
        "frames_accepted": sent, "chunks_accepted": 10, "streams": 2
    }
    assert _wav_frames_total(tmp_path) == sent


def test_stop_closes_with_every_sent_frame_and_nothing_after(master_client, monkeypatch) -> None:
    """[if] REC stops mid-stream [then] disk holds exactly the frames sent, [else stop]."""
    client, service = master_client
    monkeypatch.setattr(master_mix, "SEGMENT_SECONDS", 1)
    client.post("/api/sets/recorder/start", json={"session_id": SESSION, "source": "master", "sources": []})
    sent = 0
    for seq, frames in enumerate(_CHUNK_FRAMES):
        assert _pcm(client, seq, _tone(frames)).status_code == 204
        sent += frames
    client.post(f"/api/sets/recorder/{SESSION}/stop")
    late = _pcm(client, len(_CHUNK_FRAMES), _tone(480))
    # In flight at a clean stop: dropped, and said so without a 4xx (SET-12).
    assert (late.status_code, late.json()) == (200, {"dropped": "recording_stopped", "session_id": SESSION})
    session_dir = service.sets_root / SESSION
    events = [json.loads(line) for line in (session_dir / "timeline.jsonl").read_text().splitlines()]
    [closed] = [e["value"] for e in events if e["action"] == "master_mix_closed"]
    assert {k: closed[k] for k in ("frames_accepted", "chunks_accepted", "streams")} == {
        "frames_accepted": sent, "chunks_accepted": len(_CHUNK_FRAMES), "streams": 1
    }
    assert _wav_frames_total(session_dir) == sent
    manifest = json.loads((session_dir / "manifest.json").read_text())
    assert len(manifest["mp3_segments"]) >= 3, "SEGMENT_SECONDS=1 must have rolled"


class _FakePage:
    """Stands in for the /performance page behind the start route's push
    channel (production: the AGENT-03 order bus). ``first_chunk`` makes it act
    like a real tap: its first audio leaves the moment it is attached."""

    def __init__(self, service: RecorderService) -> None:
        self.service = service
        self.attached: list[str] = []
        self.reason: str | None = None
        self.fail_attach: str | None = None
        self.first_chunk: bytes | None = None

    def unavailable_reason(self, request) -> str | None:
        return self.reason

    async def attach(self, request, session_id: str) -> None:
        if self.fail_attach is not None:
            raise MasterTapUnavailable(self.fail_attach)
        self.attached.append(session_id)
        if self.first_chunk is not None:
            self.service.write_master_pcm(
                session_id, stream="page", seq=0, sample_rate=RATE, pcm=self.first_chunk
            )


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
    page = _FakePage(service)
    setattr(app.state, MASTER_TAP_CHANNEL_STATE, page)
    app.include_router(router)
    with TestClient(app) as client:
        client.page = page  # type: ignore[attr-defined]
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


def test_a_master_start_pushes_the_attach_and_audio_begins_at_once(master_client) -> None:
    """[if] REC starts source=master [then] the page taps before it answers, [else stop]."""
    client, service = master_client
    client.page.first_chunk = _tone(480)
    started = client.post("/api/sets/recorder/start", json={"session_id": SESSION, "source": "master", "sources": []})
    assert started.status_code == 201, started.text
    assert client.page.attached == [SESSION], "the start must push the attach, not wait for a poll"
    assert started.json()["capture"] == "recording"
    client.post(f"/api/sets/recorder/{SESSION}/stop")
    events = [json.loads(line) for line in (service.sets_root / SESSION / "timeline.jsonl").read_text().splitlines()]
    [closed] = [e["value"] for e in events if e["action"] == "master_mix_closed"]
    # Server clock, start accepted -> first captured frame. CORE's bar is ~100 ms;
    # build 15 lost 4,100-4,600 ms waiting for the rail's status poll.
    assert closed["start_to_first_frame_ms"] is not None
    assert closed["start_to_first_frame_ms"] < 100, closed


def test_a_master_start_with_no_page_starts_nothing(master_client) -> None:
    """[if] no /performance page can be asked [then] 503 and no session, [else stop]."""
    client, service = master_client
    client.page.reason = "no /performance page is open to record the master mix"
    response = client.post("/api/sets/recorder/start", json={"source": "master", "sources": []})
    assert response.status_code == 503
    assert "no /performance page is open" in response.json()["detail"]
    assert client.get("/api/sets/recorder").json()["active"] is False
    assert not service.sets_root.exists() or not any(service.sets_root.iterdir())


def test_a_master_start_whose_tap_fails_is_stopped_and_says_why(master_client) -> None:
    """[if] the page cannot connect its tap [then] 503 and REC is stopped, [else stop]."""
    client, _ = master_client
    client.page.fail_attach = "the audio engine will not run (suspended)"
    response = client.post("/api/sets/recorder/start", json={"source": "master", "sources": []})
    assert response.status_code == 503
    assert "will not run (suspended)" in response.json()["detail"]
    assert client.get("/api/sets/recorder").json()["active"] is False


def test_a_server_with_no_page_channel_refuses_master(tmp_path: Path) -> None:
    """[if] the host installed no page channel [then] master is 503, never silent, [else stop]."""
    app = FastAPI()
    app.state.sets_recorder_service = RecorderService(
        sets_root=tmp_path / "sets", db_path=tmp_path / "sets" / "sets.db", capture_enabled=False
    )
    app.include_router(router)
    with TestClient(app) as client:
        response = client.post("/api/sets/recorder/start", json={"source": "master", "sources": []})
    assert response.status_code == 503
    assert "no channel to a /performance page" in response.json()["detail"]


def test_only_a_cleanly_stopped_recording_drops_late_chunks_quietly(master_client) -> None:
    """[if] a chunk names a recording never stopped here [then] 409 stays loud, [else stop]."""
    client, _ = master_client
    assert _pcm(client, 0, _tone(480), session="2026-10-06T04-00-00").status_code == 409


def test_rec_cli_returns_only_once_audio_is_written() -> None:
    """[if] `rec start` answers while starting [then] it waits for recording or fails, [else stop]."""
    reads = iter([{"capture": "starting"}, {"capture": "recording", "session_id": SESSION}])
    status = rec_cli.wait_until_recording("u", {"capture": "starting"}, read=lambda: next(reads), sleep=lambda _s: None)
    assert status["capture"] == "recording"
    failed = iter([{"capture": "failed", "capture_error": "no master-mix audio arrived within 10 s"}])
    with pytest.raises(rec_cli.RecCommandFailed, match="no master-mix audio arrived"):
        rec_cli.wait_until_recording("u", {"capture": "starting"}, read=lambda: next(failed), sleep=lambda _s: None)
    clock = iter([0.0, 5.0, 20.0])
    with pytest.raises(rec_cli.RecCommandFailed, match="did not start within"):
        rec_cli.wait_until_recording(
            "u", {"capture": "starting"}, timeout_s=12, read=lambda: {"capture": "starting"},
            sleep=lambda _s: None, monotonic=lambda: next(clock),
        )


def test_the_order_bus_channel_attaches_through_the_leader_page() -> None:
    """[if] a master start is pushed [then] the page gets record_master_tap at once, [else stop]."""
    from types import SimpleNamespace

    from apps.webui.server.sets_master_tap import OrderBusMasterTap

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ui_mirror={"page": 1})))
    channel = OrderBusMasterTap()
    assert channel.unavailable_reason(request) is None
    assert "no /performance page" in (
        channel.unavailable_reason(SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ui_mirror=None)))) or ""
    )

    async def page(outcome: dict) -> dict:
        from apps.webui.server.routes.commands import _broker

        broker = _broker(request)
        claimed = await broker.claim_within(1.0, lambda: asyncio.sleep(0, result=False))
        assert claimed is not None
        order_id, order = claimed
        broker.complete(order_id, outcome)
        return order

    async def run(outcome: dict) -> dict:
        attached = asyncio.create_task(channel.attach(request, SESSION))
        order = await page(outcome)
        await attached
        return order

    order = asyncio.run(run({"steps": [{"status": "succeeded"}], "mirror_delta": {"changed": {}}}))
    assert order == {"kind": "single", "payload": {"type": "record_master_tap", "session_id": SESSION}}
    with pytest.raises(MasterTapUnavailable, match="will not run"):
        asyncio.run(run({"steps": [{"status": "failed", "error": "will not run"}], "mirror_delta": {"changed": {}}}))


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
from apps.sets.master_tap import MASTER_TAP_CHANNEL_STATE

app = create_app(EngineConfig(data_dir=Path(os.environ["MDT_DATA_DIR"])))
pcm = b"".join(struct.pack("<hh", v, v) for v in (int(9000 * math.sin(i / 7)) for i in range(48000)))
out = {"channel": type(getattr(app.state, MASTER_TAP_CHANNEL_STATE)).__name__}
with TestClient(app, base_url="http://127.0.0.1") as client:
    no_page = client.post("/api/sets/recorder/start", json={"source": "master", "sources": []})
    out["no_page"] = [no_page.status_code, "no /performance page is open" in no_page.text]

    class Page:
        def unavailable_reason(self, request):
            return None

        async def attach(self, request, session_id):
            out["attached"] = session_id

    setattr(app.state, MASTER_TAP_CHANNEL_STATE, Page())
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
    late = client.post(
        f"/api/sets/recorder/{sid}/master-pcm",
        params={"stream": "probe", "seq": 1, "sample_rate": 48000},
        content=pcm,
        headers={"content-type": "application/octet-stream"},
    )
out.update({"start": start.status_code, "attached_ok": out.get("attached") == sid, "chunk": chunk.status_code,
            "capture": status.get("capture"), "stop": stop.status_code, "late": [late.status_code, late.json()]})
out.pop("attached", None)
print(json.dumps(out))
"""


def test_the_packaged_engine_app_records_the_master_mix(tmp_path: Path) -> None:
    """[if] engine_core serves REC source=master [then] it pushes, records, drops late, [else stop]."""
    result = subprocess.run(
        [sys.executable, "-c", _ENGINE_PROBE],
        cwd=REPO_ROOT,
        env={**os.environ, "MDT_DATA_DIR": str(tmp_path / "engine-data")},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    out = json.loads(result.stdout.strip().splitlines()[-1])
    assert out == {
        "channel": "OrderBusMasterTap",
        "no_page": [503, True],
        "start": 201,
        "attached_ok": True,
        "chunk": 204,
        "capture": "recording",
        "stop": 200,
        "late": [200, {"dropped": "recording_stopped", "session_id": out["late"][1].get("session_id")}],
    }
