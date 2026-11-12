"""Issue #13168: master REC stop distinguishes empty input from valid silent audio.

[if] master stops with no frames vs silence or tracklist-only [then] outcomes differ, [else stop].
"""

from __future__ import annotations

import asyncio
import json
import math
import struct
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.sets import capture, master_mix, record
from apps.sets.master_tap import MASTER_TAP_CHANNEL_STATE, MasterTapUnavailable
from apps.sets.recorder_service import RecorderConflict, RecorderService
from apps.sets.state import SetsState
from tests.sets.conftest import mount_sets_router
from tests.waits import THREAD_HANG_GUARD_S

REPO_ROOT = Path(__file__).resolve().parents[2]
RATE = 48_000
SESSION = "2026-10-06T03-00-00"

_CHUNK_FRAMES = [24_000, 24_000, 17_311, 24_000, 3, 24_000, 9_999, 24_000]


def _tone(frames: int, amplitude: float = 0.5, freq: float = 440.0) -> bytes:
    out = bytearray()
    for i in range(frames):
        v = int(amplitude * 32767 * math.sin(2 * math.pi * freq * i / RATE))
        out += struct.pack("<hh", v, v)
    return bytes(out)


def _wav(path: Path) -> tuple[int, int, int]:
    with wave.open(str(path), "rb") as fh:
        return fh.getnchannels(), fh.getframerate(), fh.getnframes()


def _wav_frames_total(directory: Path) -> int:
    return sum(_wav(p)[2] for p in sorted(directory.glob("audio_*.wav")))


class _FakePage:
    """Stands in for the /performance page behind the start route's push channel."""

    def __init__(self, service: RecorderService) -> None:
        self.service = service
        self.attached: list[str] = []
        self.reason: str | None = None
        self.fail_attach: str | None = None
        self.cancel_attach = False
        self.attach_error: BaseException | None = None
        self.first_chunk: bytes | None = None

    def unavailable_reason(self, request) -> str | None:
        return self.reason

    async def attach(self, request, session_id: str) -> None:
        if self.cancel_attach:
            raise asyncio.CancelledError()
        if self.attach_error is not None:
            raise self.attach_error
        if self.fail_attach is not None:
            raise MasterTapUnavailable(self.fail_attach)
        self.attached.append(session_id)
        if self.first_chunk is not None:
            self.service.write_master_pcm(session_id, stream="page", seq=0, sample_rate=RATE, pcm=self.first_chunk)


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
    mount_sets_router(app)
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


def _assert_failed_capture_settled(recorder: record.Recorder) -> None:
    assert recorder.capture_state() == "failed"
    assert "before any audio frames were captured" in (recorder.capture_error() or "")
    assert not (recorder.session_dir / "manifest.json").exists()
    assert not list(recorder.session_dir.glob("audio_*.wav"))
    assert not (recorder.session_dir / "recorder.pid").exists()
    row = recorder.state.get_session(recorder.session_id)
    assert row is not None and row.ended_at is not None
    assert recorder._stop_event.is_set()
    assert all(not thread.is_alive() for thread in recorder._poll_threads)
    assert recorder._heartbeat_thread is None or not recorder._heartbeat_thread.is_alive()
    events = list(recorder.timeline.iter_events())
    [ended] = [event["value"] for event in events if event["action"] == "session_end"]
    assert ended == {"capture": "failed", "capture_error": recorder.capture_error()}
    [stored] = recorder.state.fetch_events(recorder.session_id, action="session_end")
    assert stored.value == ended
    [closed] = [event["value"] for event in events if event["action"] == "master_mix_closed"]
    assert closed["frames_accepted"] == closed["chunks_accepted"] == 0


@pytest.mark.requirement("SETREC-24")
@pytest.mark.parametrize("amplitude", [0.5, 0.0])
def test_stop_closes_with_every_sent_frame_and_nothing_after(master_client, monkeypatch, amplitude: float) -> None:
    """[if] REC stops mid-stream [then] disk holds exactly the frames sent, [else stop]."""
    client, service = master_client
    monkeypatch.setattr(master_mix, "SEGMENT_SECONDS", 1)
    client.post("/api/sets/recorder/start", json={"session_id": SESSION, "source": "master", "sources": []})
    sent = 0
    for seq, frames in enumerate(_CHUNK_FRAMES):
        assert _pcm(client, seq, _tone(frames, amplitude=amplitude)).status_code == 204
        sent += frames
    stopped = client.post(f"/api/sets/recorder/{SESSION}/stop")
    assert stopped.status_code == 200
    assert stopped.json()["active"] is False
    assert stopped.json()["capture"] == "none"
    late = _pcm(client, len(_CHUNK_FRAMES), _tone(480))
    assert (late.status_code, late.json()) == (200, {"dropped": "recording_stopped", "session_id": SESSION})
    session_dir = service.sets_root / SESSION
    events = [json.loads(line) for line in (session_dir / "timeline.jsonl").read_text().splitlines()]
    [closed] = [e["value"] for e in events if e["action"] == "master_mix_closed"]
    assert {k: closed[k] for k in ("frames_accepted", "chunks_accepted", "streams")} == {
        "frames_accepted": sent,
        "chunks_accepted": len(_CHUNK_FRAMES),
        "streams": 1,
    }
    assert _wav_frames_total(session_dir) == sent
    manifest = json.loads((session_dir / "manifest.json").read_text())
    assert len(manifest["mp3_segments"]) >= 3, "SEGMENT_SECONDS=1 must have rolled"


@pytest.mark.requirement("SETREC-24")
@pytest.mark.parametrize("refused_chunk", [False, True])
def test_empty_master_stop_fails_and_releases_ownership(master_client, refused_chunk: bool) -> None:
    """[if] master stops unfed [then] capture fails with its reason, [else stop]."""
    client, service = master_client
    started = client.post("/api/sets/recorder/start", json={"session_id": SESSION, "source": "master", "sources": []})
    assert started.status_code == 201
    assert client.page.attached == [SESSION]
    recorder = service._recorder
    assert recorder is not None
    if refused_chunk:
        assert _pcm(client, 0, b"malformed").status_code == 409
    stopped = client.post(f"/api/sets/recorder/{SESSION}/stop")
    assert stopped.status_code == 409, stopped.text
    failed = stopped.json()["detail"]
    assert failed == {
        "active": False,
        "session_id": SESSION,
        "pid": None,
        "owned": False,
        "recoverable": False,
        "capture": "failed",
        "capture_error": recorder.capture_error(),
        "capture_source": "master",
        "recordings_dir": str(service.sets_root),
        **service._owned_levels(recorder),
        "elapsed_s": failed["elapsed_s"],
    }
    assert failed["elapsed_s"] >= 0
    assert failed["audio_bytes"] == 0
    _assert_failed_capture_settled(recorder)
    assert service._recorder is None and service._source is None
    assert client.get("/api/sets/recorder").json()["active"] is False
    assert _pcm(client, 0, _tone(1)).status_code == 409
    other = "2026-10-06T04-00-00"
    assert (
        client.post(
            "/api/sets/recorder/start", json={"session_id": other, "source": "master", "sources": []}
        ).status_code
        == 201
    )
    assert _pcm(client, 0, bytes(master_mix.FRAME_BYTES), session=other).status_code == 204
    assert client.post(f"/api/sets/recorder/{other}/stop").status_code == 200
    assert _wav_frames_total(service.sets_root / other) == 1


@pytest.mark.requirement("SETREC-24")
@pytest.mark.parametrize("captured_first", [False, True])
def test_in_flight_chunk_is_quiet_only_after_a_successful_stop(
    master_client, monkeypatch, captured_first: bool
) -> None:
    """[if] PCM races stop [then] only a successful capture drops it quietly, [else stop]."""
    client, service = master_client
    assert (
        client.post(
            "/api/sets/recorder/start", json={"session_id": SESSION, "source": "master", "sources": []}
        ).status_code
        == 201
    )
    recorder = service._recorder
    assert recorder is not None and recorder._master is not None
    if captured_first:
        assert _pcm(client, 0, _tone(1)).status_code == 204
    append = recorder._master.append
    entered, release = threading.Event(), threading.Event()

    def delayed_append(*, stream: str, seq: int, sample_rate: int, pcm: bytes) -> None:
        entered.set()
        if not release.wait(THREAD_HANG_GUARD_S):
            raise TimeoutError("stop never released the in-flight PCM chunk")
        append(stream=stream, seq=seq, sample_rate=sample_rate, pcm=pcm)

    monkeypatch.setattr(recorder._master, "append", delayed_append)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            service.write_master_pcm, SESSION, stream="tab-1", seq=int(captured_first), sample_rate=RATE, pcm=_tone(1)
        )
        try:
            assert entered.wait(THREAD_HANG_GUARD_S)
            stopped = client.post(f"/api/sets/recorder/{SESSION}/stop")
            assert stopped.status_code == (200 if captured_first else 409)
        finally:
            release.set()
        expected = master_mix.MasterMixRecordingStopped if captured_first else RecorderConflict
        with pytest.raises(expected):
            pending.result(timeout=THREAD_HANG_GUARD_S)


@pytest.mark.requirement("SETREC-24")
def test_empty_master_low_level_stop_settles_without_success_manifest(tmp_path: Path) -> None:
    """[if] low-level master stops unfed [then] it settles as failed, [else stop]."""
    recorder = record.start(
        session_id=SESSION,
        config=record.RecorderConfig(sources=(), master_mix=True),
        sets_root=tmp_path / "sets",
        state=SetsState(db_path=tmp_path / "sets.db"),
    )
    recorder.start_threads()
    assert record.stop(recorder) is None
    _assert_failed_capture_settled(recorder)


@pytest.mark.requirement("SETREC-24")
def test_empty_master_shutdown_settles_without_success_manifest(master_client) -> None:
    """[if] daemon shuts down an unfed master [then] cleanup records failure, [else stop]."""
    client, service = master_client
    assert (
        client.post(
            "/api/sets/recorder/start", json={"session_id": SESSION, "source": "master", "sources": []}
        ).status_code
        == 201
    )
    recorder = service._recorder
    assert recorder is not None
    service.stop_owned_on_shutdown()
    _assert_failed_capture_settled(recorder)
    assert service._recorder is None and service._source is None
    assert _pcm(client, 0, _tone(1)).status_code == 409


@pytest.mark.requirement("SETREC-24")
def test_tracklist_only_stop_still_succeeds_without_audio(master_client) -> None:
    """[if] tracklist-only stops without PCM [then] a valid manifest is written, [else stop]."""
    from jsonschema import Draft202012Validator

    client, service = master_client
    assert (
        client.post(
            "/api/sets/recorder/start", json={"session_id": SESSION, "source": "none", "sources": []}
        ).status_code
        == 201
    )
    stopped = client.post(f"/api/sets/recorder/{SESSION}/stop")
    assert stopped.status_code == 200
    assert stopped.json()["capture"] == "none"
    manifest = json.loads((service.sets_root / SESSION / "manifest.json").read_text(encoding="utf-8"))
    schema = json.loads((REPO_ROOT / "apps/sets/schemas/session_manifest.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(manifest)
    assert manifest["mp3_segments"] == []
    assert all(manifest[key] is None for key in ("audio_peak_dbfs", "clipped_samples", "clip_events"))
