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
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.sets import capture, master_mix, rec_cli, record
from apps.sets.__main__ import main as sets_main
from apps.sets.master_tap import MASTER_TAP_CHANNEL_STATE, MasterTapUnavailable
from apps.sets.recorder_service import RecorderConflict, RecorderService
from apps.sets.state import SetsState
from tests.sets.conftest import mount_sets_router
from tests.waits import THREAD_HANG_GUARD_S

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


# Issue #12407: abrupt MasterMixWriter.close() must leave each segment's RIFF header accurate.


def test_abrupt_close_after_one_chunk_retains_accurate_wav_header(tmp_path: Path) -> None:
    """[if] writer closes after one chunk [then] WAV header frames match, [else stop]."""
    writer = _writer(tmp_path)
    frames = 480
    writer.append(stream="s1", seq=0, sample_rate=RATE, pcm=_tone(frames))
    writer.close()
    segments = sorted(tmp_path.glob("audio_*.wav"))
    assert len(segments) == 1
    assert _wav(segments[0]) == (2, RATE, frames)
    assert writer.current_state() == "stopped"


def test_abrupt_close_after_segment_roll_retains_each_segment_header(tmp_path: Path) -> None:
    """[if] writer closes after a roll [then] each segment header matches, [else stop]."""
    writer = _writer(tmp_path, segment_seconds=1)
    writer.append(stream="s", seq=0, sample_rate=RATE, pcm=_tone(RATE))
    writer.append(stream="s", seq=1, sample_rate=RATE, pcm=_tone(7_311))
    writer.close()
    assert len(list(tmp_path.glob("audio_*.wav"))) == 2
    assert _wav_frames_per_segment(tmp_path) == [RATE, 7_311]


def test_abrupt_close_after_stream_change_retains_each_segment_header(tmp_path: Path) -> None:
    """[if] stream changes then writer closes [then] each segment header matches, [else stop]."""
    writer = _writer(tmp_path)
    writer.append(stream="a", seq=0, sample_rate=RATE, pcm=_tone(480))
    writer.append(stream="b", seq=0, sample_rate=RATE, pcm=_tone(441))
    writer.close()
    segments = sorted(tmp_path.glob("audio_*.wav"))
    assert len(segments) == 2
    assert _wav_frames_per_segment(tmp_path) == [480, 441]
    assert [_wav(p)[1:] for p in segments] == [(RATE, 480), (RATE, 441)]


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


def _wav_frames_per_segment(directory: Path) -> list[int]:
    return [_wav(p)[2] for p in sorted(directory.glob("audio_*.wav"))]


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
        "frames_accepted": sent,
        "chunks_accepted": 10,
        "streams": 2,
    }
    assert _wav_frames_total(tmp_path) == sent


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
    # In flight at a clean stop: dropped, and said so without a 4xx (SET-12).
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


class _FakePage:
    """Stands in for the /performance page behind the start route's push
    channel (production: the AGENT-03 order bus). ``first_chunk`` makes it act
    like a real tap: its first audio leaves the moment it is attached."""

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


def _session_dirs(service: RecorderService) -> list[Path]:
    root = service.sets_root
    if not root.exists():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / "timeline.jsonl").is_file())


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
    assert closed["start_to_tap_attached_ms"] is not None and closed["start_to_tap_attached_ms"] < 100, closed


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
    client, service = master_client
    client.page.fail_attach = "the audio engine will not run (suspended)"
    response = client.post("/api/sets/recorder/start", json={"source": "master", "sources": []})
    assert response.status_code == 503
    assert "will not run (suspended)" in response.json()["detail"]
    assert client.get("/api/sets/recorder").json()["active"] is False
    assert service._recorder is None and service._source is None
    [session_dir] = _session_dirs(service)
    assert not (session_dir / "manifest.json").exists()
    assert not (session_dir / "recorder.pid").exists()
    events = [json.loads(line) for line in (session_dir / "timeline.jsonl").read_text().splitlines()]
    [failed] = [e for e in events if e["action"] == "master_tap_failed"]
    assert "will not run (suspended)" in failed["value"]["reason"]
    assert failed["value"]["phase"] == "attach"


def test_a_cancelled_master_attach_finalizes_with_a_timeline_reason(master_client) -> None:
    """[if] attach is cancelled during startup [then] timeline records cancel, [else stop]."""
    client, service = master_client
    client.page.cancel_attach = True
    with TestClient(client.app, raise_server_exceptions=False) as soft:
        soft.page = client.page  # type: ignore[attr-defined]
        response = soft.post("/api/sets/recorder/start", json={"source": "master", "sources": []})
    assert response.status_code in {500, 503}
    assert service._recorder is None
    assert client.get("/api/sets/recorder").json()["active"] is False
    session_dirs = _session_dirs(service)
    assert len(session_dirs) == 1
    events = [json.loads(line) for line in (session_dirs[0] / "timeline.jsonl").read_text().splitlines()]
    [failed] = [e for e in events if e["action"] == "master_tap_failed"]
    assert "cancelled" in failed["value"]["reason"].lower()
    assert failed["value"]["phase"] == "attach"


def test_a_master_attach_runtime_error_finalizes_with_a_timeline_reason(master_client) -> None:
    """[if] attach raises outside MasterTapUnavailable [then] session ends with reason, [else stop]."""
    client, service = master_client
    client.page.attach_error = RuntimeError("broker down")
    with TestClient(client.app, raise_server_exceptions=False) as soft:
        soft.page = client.page  # type: ignore[attr-defined]
        response = soft.post("/api/sets/recorder/start", json={"source": "master", "sources": []})
    assert response.status_code == 500
    assert service._recorder is None
    session_dirs = _session_dirs(service)
    assert len(session_dirs) == 1
    events = [json.loads(line) for line in (session_dirs[0] / "timeline.jsonl").read_text().splitlines()]
    [failed] = [e for e in events if e["action"] == "master_tap_failed"]
    assert "broker down" in failed["value"]["reason"]


def test_a_successful_master_start_has_no_master_tap_failed_event(master_client) -> None:
    """[if] attach succeeds and audio lands [then] no master_tap_failed, [else stop]."""
    client, service = master_client
    client.post("/api/sets/recorder/start", json={"session_id": SESSION, "source": "master", "sources": []})
    assert _pcm(client, 0, _tone(480)).status_code == 204
    events = [json.loads(line) for line in (service.sets_root / SESSION / "timeline.jsonl").read_text().splitlines()]
    assert [e for e in events if e["action"] == "master_tap_failed"] == []


def test_a_server_with_no_page_channel_refuses_master(tmp_path: Path) -> None:
    """[if] the host installed no page channel [then] master is 503, never silent, [else stop]."""
    app = FastAPI()
    app.state.sets_recorder_service = RecorderService(
        sets_root=tmp_path / "sets", db_path=tmp_path / "sets" / "sets.db", capture_enabled=False
    )
    mount_sets_router(app)
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
            "u",
            {"capture": "starting"},
            timeout_s=12,
            read=lambda: {"capture": "starting"},
            sleep=lambda _s: None,
            monotonic=lambda: next(clock),
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
        f"/api/sets/recorder/{SESSION}/master-pcm",
        params={"seq": 0},
        content=_tone(480),
        headers={"content-type": "application/octet-stream"},
    )
    assert response.status_code == 422


@pytest.mark.requirement("SETREC-22")
def test_master_pcm_accepts_octet_stream_content_type(master_client) -> None:
    """[if] octet-stream PCM posts to master-pcm [then] 204, [else stop]."""
    client, _ = master_client
    client.post(
        "/api/sets/recorder/start",
        json={"session_id": SESSION, "source": "master", "sources": []},
    )
    assert _pcm(client, 0, _tone(480)).status_code == 204


@pytest.mark.requirement("SETREC-22")
def test_master_pcm_rejects_json_content_type(master_client, monkeypatch) -> None:
    """[if] JSON targets master-pcm [then] 415, [else stop]."""
    client, service = master_client
    writes: list[object] = []
    original = service.write_master_pcm

    def tracked(session_id: str, **kwargs: object) -> None:
        writes.append(session_id)
        original(session_id, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "write_master_pcm", tracked)
    client.post(
        "/api/sets/recorder/start",
        json={"session_id": SESSION, "source": "master", "sources": []},
    )
    response = client.post(
        f"/api/sets/recorder/{SESSION}/master-pcm",
        params={"stream": "tab-1", "seq": 0, "sample_rate": RATE},
        content=b"{}",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 415
    assert "application/octet-stream" in response.json()["detail"]
    assert writes == []


@pytest.mark.requirement("SETREC-22")
def test_master_pcm_rejects_missing_content_type(master_client, monkeypatch) -> None:
    """[if] Content-Type is omitted [then] 415, [else stop]."""
    client, service = master_client
    writes: list[object] = []
    original = service.write_master_pcm

    def tracked(session_id: str, **kwargs: object) -> None:
        writes.append(session_id)
        original(session_id, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "write_master_pcm", tracked)
    client.post(
        "/api/sets/recorder/start",
        json={"session_id": SESSION, "source": "master", "sources": []},
    )
    response = client.post(
        f"/api/sets/recorder/{SESSION}/master-pcm",
        params={"stream": "tab-1", "seq": 0, "sample_rate": RATE},
        content=_tone(480),
    )
    assert response.status_code == 415
    assert writes == []


@pytest.mark.parametrize(
    ("source", "device"),
    [("loopback", "MacBook Pro Microphone"), ("external", "BlackHole 2ch")],
)
def test_a_source_that_contradicts_its_input_is_422(master_client, source: str, device: str) -> None:
    """[if] loopback names a mic or external a loopback [then] 422, nothing starts, [else stop]."""
    client, _ = master_client
    response = client.post("/api/sets/recorder/start", json={"source": source, "device_name": device, "sources": []})
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


def test_a_tap_still_streaming_into_a_stopped_recording_is_logged_at_info(master_client, caplog) -> None:
    """[if] a stopped recording keeps receiving chunks [then] an INFO canary names it, [else stop]."""
    import logging

    from apps.sets import recorder_service

    client, _ = master_client
    client.post("/api/sets/recorder/start", json={"session_id": SESSION, "source": "master", "sources": []})
    assert _pcm(client, 0, _tone(480)).status_code == 204
    client.post(f"/api/sets/recorder/{SESSION}/stop")
    caplog.set_level(logging.INFO, logger=recorder_service.__name__)
    for seq in range(1, recorder_service.LATE_CHUNK_CANARY + 1):
        assert _pcm(client, seq, _tone(480)).status_code == 200
    assert [r for r in caplog.records if "late chunks" in r.getMessage()] == [], "in-flight stragglers are quiet"
    for seq in range(recorder_service.LATE_CHUNK_CANARY + 1, 9):
        assert _pcm(client, seq, _tone(480)).status_code == 200
    canaries = [r.getMessage() for r in caplog.records if "late chunks" in r.getMessage()]
    assert canaries == [
        f"master mix: 3 late chunks for stopped recording {SESSION}; a page tap is still streaming",
        f"master mix: 4 late chunks for stopped recording {SESSION}; a page tap is still streaming",
        f"master mix: 8 late chunks for stopped recording {SESSION}; a page tap is still streaming",
    ]
    assert all(r.levelno == logging.INFO for r in caplog.records if "late chunks" in r.getMessage())


def test_a_start_that_times_out_on_the_tap_pushes_a_cancel_after_the_attach(monkeypatch) -> None:
    """[if] the page connects its tap only after the start gave up [then] a cancel order follows, [else stop]."""
    from types import SimpleNamespace

    from apps.webui.server import sets_master_tap
    from apps.webui.server.routes.commands import _broker

    monkeypatch.setattr(sets_master_tap, "ATTACH_DEADLINE_S", 0.05)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ui_mirror={"page": 1})))
    channel = sets_master_tap.OrderBusMasterTap()
    never = lambda: asyncio.sleep(0, result=False)  # noqa: E731

    async def run() -> list[dict]:
        broker = _broker(request)
        attached = asyncio.create_task(channel.attach(request, SESSION))
        attach_id, attach_order = await broker.claim_within(1.0, never)
        with pytest.raises(MasterTapUnavailable, match="did not connect its tap"):
            await attached
        cancel_id, cancel_order = await broker.claim_within(1.0, never)
        # The slow page finishes both orders late; neither result is refused,
        # so the page's order loop survives (a withdrawn order 404s it).
        broker.complete(attach_id, {"steps": [{"status": "succeeded"}], "mirror_delta": {"changed": {}}})
        broker.complete(cancel_id, {"steps": [{"status": "succeeded"}], "mirror_delta": {"changed": {}}})
        await asyncio.sleep(0)
        return [attach_order, cancel_order]

    attach_order, cancel_order = asyncio.run(run())
    assert attach_order["payload"] == {"type": "record_master_tap", "session_id": SESSION}
    assert cancel_order == {"kind": "single", "payload": {"type": "record_master_tap_cancel", "session_id": SESSION}}


# -- level accounting (issue #10371) -------------------------------------------

_FS = 32767


def _frames(pairs: list[tuple[int, int]]) -> bytes:
    return b"".join(struct.pack("<hh", left, right) for left, right in pairs)


def _quiet_with_runs(total: int, runs: list[tuple[int, int]], both: bool = True) -> bytes:
    """``total`` quiet frames with full-scale runs at (start, length)."""
    pairs = [(100, 100)] * total
    for start, length in runs:
        for i in range(start, start + length):
            pairs[i] = (_FS, _FS) if both else (_FS, 100)
    return _frames(pairs)


@pytest.mark.requirement("SET-11744")
def test_a_minus_6_dbfs_sine_reads_minus_6_with_no_clips(tmp_path: Path) -> None:
    """[if] a -6 dBFS sine does not read -6.0 with zero clips [then] broken."""
    writer = _writer(tmp_path)
    writer.append(stream="s", seq=0, sample_rate=RATE, pcm=_tone(4096, amplitude=10 ** (-6 / 20)))
    levels = writer.levels()
    assert levels["peak_dbfs"] == pytest.approx(-6.0, abs=0.1)
    assert (levels["clipped_samples"], levels["clip_events"]) == (0, 0)


@pytest.mark.requirement("SET-11744")
@pytest.mark.parametrize(("frames", "both"), [(200, True), (400, False)])
def test_one_400_sample_full_scale_run_is_one_clip_event(tmp_path: Path, frames: int, both: bool) -> None:
    """[if] 400 full-scale samples in one run are not 400 clipped samples and 1 event [then] broken."""
    writer = _writer(tmp_path)
    writer.append(stream="s", seq=0, sample_rate=RATE, pcm=_quiet_with_runs(4096, [(1000, frames)], both))
    levels = writer.levels()
    assert (levels["clipped_samples"], levels["clip_events"], levels["peak_dbfs"]) == (400, 1, 0.0)


@pytest.mark.requirement("SET-11744")
def test_two_separated_runs_are_two_events_and_a_run_of_two_is_none(tmp_path: Path) -> None:
    """[if] two separated runs are not 2 events, or a 2-frame run counts [then] broken."""
    writer = _writer(tmp_path)
    writer.append(stream="s", seq=0, sample_rate=RATE, pcm=_quiet_with_runs(500, [(10, 10), (120, 10)]))
    assert writer.levels()["clip_events"] == 2
    short = _writer(tmp_path / "short")
    short.append(stream="s", seq=0, sample_rate=RATE, pcm=_quiet_with_runs(500, [(10, 2)]))
    assert (short.levels()["clipped_samples"], short.levels()["clip_events"]) == (4, 0)


@pytest.mark.requirement("SET-11744")
def test_a_run_split_across_chunks_counts_once_but_not_across_streams(tmp_path: Path) -> None:
    """[if] a run split over two chunks is not 1 event, or one split by a new stream is not 0 [then] broken."""
    tail, head = _quiet_with_runs(100, [(98, 2)]), _quiet_with_runs(100, [(0, 2)])
    same = _writer(tmp_path / "same")
    same.append(stream="s", seq=0, sample_rate=RATE, pcm=tail)
    same.append(stream="s", seq=1, sample_rate=RATE, pcm=head)
    assert same.levels()["clip_events"] == 1
    split = _writer(tmp_path / "split")
    split.append(stream="a", seq=0, sample_rate=RATE, pcm=tail)
    split.append(stream="b", seq=0, sample_rate=RATE, pcm=head)
    assert split.levels()["clip_events"] == 0


@pytest.mark.requirement("SET-11744")
def test_recent_peak_forgets_after_3_s_of_audio(tmp_path: Path) -> None:
    """[if] peak_dbfs_recent still shows a peak older than 3 s of audio [then] broken."""
    writer = _writer(tmp_path)
    writer.append(stream="s", seq=0, sample_rate=RATE, pcm=_tone(4800, amplitude=1.0))
    quiet = _tone(24_000, amplitude=10 ** (-20 / 20))
    for seq in range(1, 8):
        writer.append(stream="s", seq=seq, sample_rate=RATE, pcm=quiet)
    levels = writer.levels()
    assert levels["peak_dbfs"] == pytest.approx(0.0, abs=0.1)
    assert levels["peak_dbfs_recent"] == pytest.approx(-20.0, abs=0.1)


@pytest.mark.requirement("SET-11744")
def test_level_accounting_costs_under_10_ms_per_chunk() -> None:
    """[if] accounting 1000 chunks of 4096 stereo frames costs 10 ms or more per chunk [then] broken."""
    import time

    chunk = _quiet_with_runs(4096, [(100, 5), (2000, 5)])
    acc = master_mix.LevelAccumulator()
    t0 = time.perf_counter()
    for _ in range(1000):
        acc.add(chunk, RATE)
    mean_ms = time.perf_counter() - t0  # 1000 chunks: seconds == ms per chunk
    print(f"level accounting: {mean_ms:.3f} ms per chunk")
    assert mean_ms < 10.0
    assert acc.clip_events == 2000


@pytest.mark.requirement("SET-11744")
def test_refused_chunk_changes_no_levels(tmp_path: Path) -> None:
    """[if] a refused chunk changes the level counters [then] broken."""
    writer = _writer(tmp_path)
    writer.append(stream="s", seq=0, sample_rate=RATE, pcm=_tone(480))
    before = writer.levels()
    with pytest.raises(master_mix.MasterMixChunkRefused):
        writer.append(stream="s", seq=5, sample_rate=RATE, pcm=_quiet_with_runs(100, [(0, 50)]))
    assert writer.levels() == before


@pytest.mark.requirement("SET-11744")
def test_recorder_status_carries_peak_and_clips(master_client) -> None:
    """[if] GET /api/sets/recorder lacks the level fields, or a tracklist-only one is non-null [then] broken."""
    client, _ = master_client
    client.post("/api/sets/recorder/start", json={"session_id": SESSION, "source": "master", "sources": []})
    assert _pcm(client, 0, _quiet_with_runs(4800, [(0, 10)])).status_code == 204
    status = client.get("/api/sets/recorder").json()
    assert (status["peak_dbfs"], status["peak_dbfs_recent"]) == (0.0, 0.0)
    assert (status["clipped_samples"], status["clip_events"]) == (20, 1)
    assert status["audio_bytes"] == 4800 * master_mix.FRAME_BYTES
    assert status["elapsed_s"] >= 0
    client.post(f"/api/sets/recorder/{SESSION}/stop")
    idle = client.get("/api/sets/recorder").json()
    assert all(idle[k] is None for k in ("peak_dbfs", "clipped_samples", "clip_events", "elapsed_s"))
    client.post("/api/sets/recorder/start", json={"session_id": "2026-10-06T04-00-00", "source": "none", "sources": []})
    tracklist = client.get("/api/sets/recorder").json()
    assert tracklist["active"] is True
    assert all(tracklist[k] is None for k in ("peak_dbfs", "peak_dbfs_recent", "clipped_samples", "clip_events"))
    assert tracklist["elapsed_s"] is not None


@pytest.mark.requirement("SET-11744")
def test_stop_writes_levels_into_a_schema_valid_manifest(master_client) -> None:
    """[if] the stop manifest lacks the levels or fails the schema, or a tracklist one is non-null [then] broken."""
    from jsonschema import Draft202012Validator

    schema = json.loads((REPO_ROOT / "apps/sets/schemas/session_manifest.schema.json").read_text())
    client, service = master_client
    client.post("/api/sets/recorder/start", json={"session_id": SESSION, "source": "master", "sources": []})
    _pcm(client, 0, _quiet_with_runs(4800, [(0, 10), (100, 10), (200, 10)]))
    client.post(f"/api/sets/recorder/{SESSION}/stop")
    manifest = json.loads((service.sets_root / SESSION / "manifest.json").read_text())
    Draft202012Validator(schema).validate(manifest)
    assert (manifest["audio_peak_dbfs"], manifest["clipped_samples"], manifest["clip_events"]) == (0.0, 60, 3)
    events = [json.loads(line) for line in (service.sets_root / SESSION / "timeline.jsonl").read_text().splitlines()]
    [closed] = [e["value"] for e in events if e["action"] == "master_mix_closed"]
    assert closed["clip_events"] == 3
    other = "2026-10-06T04-00-00"
    client.post("/api/sets/recorder/start", json={"session_id": other, "source": "none", "sources": []})
    client.post(f"/api/sets/recorder/{other}/stop")
    plain = json.loads((service.sets_root / other / "manifest.json").read_text())
    Draft202012Validator(schema).validate(plain)
    assert (plain["audio_peak_dbfs"], plain["clipped_samples"], plain["clip_events"]) == (None, None, None)
