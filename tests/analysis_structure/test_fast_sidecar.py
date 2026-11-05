"""Own-fast sections served from a real decoded track (STRUCT-06).

[if] the own-fast flag is on and no All-In-One sections exist [then] /anlz serves own-fast sections or answers pending at once, [else stop].

Acceptance lines:
  [if] the flag is on, rekordbox has no PSSI and All-In-One has no sidecar
       [then] /anlz serves own-fast sections cut on the payload's own downbeats,
       phrases_source own-fast ⛔️ an empty list while a fast answer exists
  [if] an ok All-In-One sidecar exists [then] it wins over own-fast
       ⛔️ the faster, worse source shadowing the better one
  [if] the flag is off [then] /anlz is unchanged (phrases_source none)
       ⛔️ own-fast work on the load path for everyone
  [if] rekordbox PSSI exists under the rbx lane [then] it is served untouched
       ⛔️ own-fast replacing vendor phrases
  [if] the grid changes [then] the sections are recut on the new grid
       ⛔️ section lines on bars the deck no longer draws
  [if] the cache file is corrupt [then] it is quarantined, kept, and recomputed;
       unreadable [then] reported failed and left untouched ⛔️ evidence destroyed
  [if] the decoder stays busy past BUSY_RETRIES, or the audio cannot be
       decoded now [then] a failed answer is announced and held in memory, never
       written ⛔️ a deck left pending forever, or a transient failure cached forever
  [if] the cached peaks are for replaced audio [then] the load answers pending
       at once ⛔️ a decode on the load path
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
import wave
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from apps.adapters.rekordbox import config as rb_config
from apps.analysis_structure import fast_sidecar
from apps.analysis_structure.contract import OwnPhrasesStatus
from apps.analysis_structure.pipeline import build_sidecar, write_sidecar
from apps.analysis_structure.serve import merge_own_phrases
from apps.analysis_waveform import decode, local_waveform
from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter

pytestmark = [pytest.mark.requirement("STRUCT-06"), pytest.mark.requires_ffmpeg]

SID = "f" * 40
RATE = 44_100
BAR_S = 2.0  # 120 BPM
# (bars, bass 55 Hz, mids 1 kHz, hats 10 kHz) amplitudes as fractions of full scale.
PLAN = [(16, 0.05, 0.10, 0.02), (16, 0.6, 0.3, 0.3), (16, 0.01, 0.3, 0.01), (16, 0.6, 0.3, 0.3)]


def _write_arc(path: Path) -> float:
    """A real WAV whose bands change at bars 16, 32 and 48, with a beat pulse."""
    chunks = []
    for bars, bass, mids, hats in PLAN:
        t = np.arange(int(bars * BAR_S * RATE)) / RATE
        pulse = 0.5 + 0.5 * (np.mod(t, BAR_S / 4) < 0.1)
        x = (
            bass * pulse * np.sin(2 * math.pi * 55 * t)
            + mids * np.sin(2 * math.pi * 1000 * t)
            + hats * pulse * np.sin(2 * math.pi * 10_000 * t)
        )
        chunks.append(x)
    audio = (np.concatenate(chunks) * 32_000).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(audio.tobytes())
    return len(audio) / RATE


@pytest.fixture
def track(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> float:
    state_path = tmp_path / "state.db"
    monkeypatch.setattr(rb_config, "STATE_DB", state_path)
    monkeypatch.setattr(rb_config, "MASTER_PLAIN_DB", tmp_path / "absent.db")
    monkeypatch.setattr(rb_config, "LOCAL_WAVEFORM_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv(fast_sidecar.ENV_FLAG, "1")
    audio = tmp_path / "arc.wav"
    duration = _write_arc(audio)
    conn = state_db.open_rw(state_path)
    try:
        writer = StateWriter(conn, actor="unit-test")
        writer.upsert_track(
            stable_id=SID, stable_id_tier="inferred", title="Arc", artists=["X"], album=None,
            isrc=None, duration_ms=int(duration * 1000), file_path=str(audio),
        )  # fmt: skip
        writer.close()
    finally:
        conn.close()
    return duration


def _payload(offset_s: float = 0.0, bars: int = 64) -> dict:
    beats = [{"n": 1 + k % 4, "bpm": 120.0, "t": round(offset_s + k * BAR_S / 4, 3)} for k in range(bars * 4)]
    return {"phrases": [], "beatgrid": {"source": "rekordbox", "beats": beats}}


def _served(payload: dict, data_dir: Path, source: str = "rbx") -> dict:
    """Serve until own-fast is no longer pending; the decode runs on the worker."""
    deadline = time.monotonic() + 30
    while True:
        out = merge_own_phrases(payload, SID, data_dir, source=source)
        if out.get("own_phrases", {}).get("status") != "pending" or time.monotonic() > deadline:
            return out
        time.sleep(0.05)


def test_own_fast_fills_an_empty_pssi_on_the_payload_grid(track: float, tmp_path: Path):
    out = _served(_payload(), tmp_path)
    assert out["phrases_source"] == "own-fast"
    assert out["own_phrases"]["status"] == "ok"
    assert out["own_phrases"]["producer"] == "own-fast"
    assert [p["start_s"] for p in out["phrases"]] == [0.0, 32.0, 64.0, 96.0]
    assert all(p["source"] == "own-fast" for p in out["phrases"])
    assert [p["label"] for p in out["phrases"]] == ["intro", "drop", "breakdown", "drop"]
    # STRUCT-07: the drop and break moments ride along on the same payload.
    moments = [(m["kind"], m["time_s"], m.get("number")) for m in out["phrase_moments"]]
    assert moments == [("drop", 32.0, 1), ("break", 64.0, None), ("drop", 96.0, 2)]


def test_the_own_lane_takes_own_fast_too(track: float, tmp_path: Path):
    out = _served(_payload(), tmp_path, source="own")
    assert out["phrases_source"] == "own-fast"


def test_a_cached_answer_is_reused_and_a_new_grid_recuts(track: float, tmp_path: Path):
    first = _served(_payload(), tmp_path)
    stamp = fast_sidecar.fast_path(tmp_path, SID).stat().st_mtime_ns
    again = merge_own_phrases(_payload(), SID, tmp_path, source="rbx")
    assert again["phrases"] == first["phrases"]
    assert fast_sidecar.fast_path(tmp_path, SID).stat().st_mtime_ns == stamp
    # The grid moved by one beat: every section start moves with it.
    shifted = _served(_payload(offset_s=0.5, bars=63), tmp_path)
    assert [p["start_s"] for p in shifted["phrases"]] == [0.5, 32.5, 64.5, 96.5]


def test_an_all_in_one_sidecar_wins_over_own_fast(track: float, tmp_path: Path):
    raw = {
        "status": "ok",
        "segments": [
            {"start": 0.0, "end": 40.0, "label": "intro"},
            {"start": 40.0, "end": 128.0, "label": "chorus"},
        ],
    }
    grid = [BAR_S * i for i in range(64)]
    write_sidecar(tmp_path, build_sidecar(SID, raw, grid, {"backend": "b", "backend_version": "1"}))
    out = merge_own_phrases(_payload(), SID, tmp_path, source="rbx")
    assert out["phrases_source"] == "own"
    assert [p["start_s"] for p in out["phrases"]] == [0.0, 40.0]
    assert "phrase_moments" in out and out["own_phrases"]["producer"] != "own-fast"


def test_rekordbox_pssi_is_untouched(track: float, tmp_path: Path):
    pssi = [{"start_s": 0.0, "end_s": 30.0, "kind": 1, "mood": 2}]
    out = merge_own_phrases({**_payload(), "phrases": pssi}, SID, tmp_path, source="rbx")
    assert out["phrases"] == pssi and out["phrases_source"] == "rekordbox"
    assert not fast_sidecar.fast_path(tmp_path, SID).exists()


def test_flag_off_serves_exactly_what_it_did_before(track: float, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(fast_sidecar.ENV_FLAG)
    out = merge_own_phrases(_payload(), SID, tmp_path, source="rbx")
    assert out["phrases"] == [] and out["phrases_source"] == "none"
    assert "own_phrases" not in out
    assert not fast_sidecar.fast_path(tmp_path, SID).exists()


def test_a_track_that_needs_a_decode_answers_pending_at_once(track: float, tmp_path: Path):
    started = time.monotonic()
    first = merge_own_phrases(_payload(), SID, tmp_path, source="rbx")
    elapsed = time.monotonic() - started
    assert first["own_phrases"]["status"] == "pending"
    assert OwnPhrasesStatus(**first["own_phrases"]).status == "pending", "the versioned contract accepts it"
    assert first["phrases"] == [] and first["phrases_source"] == "none"
    assert elapsed < fast_sidecar.INLINE_WAIT_S, "a decode must never hold the load"
    # Control: once the peaks are cached, a new grid is cut inline, no pending answer.
    _served(_payload(), tmp_path)
    inline = merge_own_phrases(_payload(offset_s=0.5, bars=63), SID, tmp_path, source="rbx")
    assert inline["own_phrases"]["status"] == "ok"


def _quarantined(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.corrupt-*"))


def test_a_corrupt_cache_file_is_quarantined_then_recomputed(track: float, tmp_path: Path):
    path = fast_sidecar.fast_path(tmp_path, SID)
    path.parent.mkdir(parents=True, exist_ok=True)
    truncated = '{"schema": "structure-fast/v1", "sections": ['  # truncated write
    path.write_text(truncated, encoding="utf-8", newline="\n")
    out = _served(_payload(), tmp_path)
    assert out["phrases_source"] == "own-fast" and out["own_phrases"]["status"] == "ok"
    assert json.loads(path.read_text(encoding="utf-8"))["status"] == "ok"
    kept = _quarantined(path)
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == truncated, "the corrupt bytes must survive"
    # A foreign document is corrupt too: kept aside, not overwritten in place.
    path.write_text(json.dumps({"hello": "world"}), encoding="utf-8", newline="\n")
    assert _served(_payload(), tmp_path)["own_phrases"]["status"] == "ok"
    assert len(_quarantined(path)) == 2
    # Control: an OLDER structure-fast schema is a version change, recomputed without quarantine.
    path.write_text(json.dumps({"schema": "structure-fast/v0"}), encoding="utf-8", newline="\n")
    assert _served(_payload(), tmp_path)["own_phrases"]["status"] == "ok"
    assert json.loads(path.read_text(encoding="utf-8"))["schema"] == fast_sidecar.SCHEMA
    assert len(_quarantined(path)) == 2


_SECTION = {"start_s": 0.0, "end_s": 30.0, "label": "intro", "start_bar": 0}  # one well-formed section


@pytest.mark.parametrize(
    "bad",
    [
        {"stable_id": "e" * 40, "status": "ok", "sections": [_SECTION], "moments": []},  # another track's
        {"stable_id": SID, "sections": [_SECTION], "moments": []},  # no status
        {"stable_id": SID, "status": "ok"},  # ok without sections
        {"stable_id": SID, "status": "ok", "sections": [None], "moments": []},  # a null section
        {"stable_id": SID, "status": "ok", "sections": [_SECTION], "moments": [{"kind": "drop"}]},  # a partial moment
        {"stable_id": SID, "status": "ok", "sections": [_SECTION], "moments": [], "producer": None},  # no producer
        {
            "stable_id": SID,
            "status": "ok",
            "sections": [_SECTION],
            "moments": [],
            "producer": "allin1",
        },  # another producer
        {"stable_id": SID, "status": "ok", "sections": [], "moments": []},  # ok with no sections
        {"stable_id": SID, "status": "ok", "sections": [{**_SECTION, "start_s": "bad"}], "moments": []},  # text bound
        {"stable_id": SID, "status": "ok", "sections": [{**_SECTION, "end_s": float("nan")}], "moments": []},  # NaN
        {"stable_id": SID, "status": "ok", "sections": [{**_SECTION, "end_s": -1.0}], "moments": []},  # ends first
        {"stable_id": SID, "status": "ok", "sections": [{**_SECTION, "label": 7}], "moments": []},  # numeric label
        {
            "stable_id": SID,
            "status": "ok",
            "sections": [_SECTION],
            "moments": [{"kind": "drop", "time_s": None, "bar": 0}],
        },  # moment without a time
        {"stable_id": SID, "status": "ok", "sections": [{**_SECTION, "bars": "16"}], "moments": []},  # text bars
        {"stable_id": SID, "status": "ok", "sections": [{**_SECTION, "start_bar": True}], "moments": []},  # bool bar
        {"stable_id": SID, "status": "ok", "sections": [{**_SECTION, "dj_label": 3}], "moments": []},  # numeric dj
        {"stable_id": SID, "status": "ok", "sections": [{**_SECTION, "ambient": "yes"}], "moments": []},  # text flag
        {"stable_id": SID, "status": "ok", "sections": [{**_SECTION, "energy": {"score_db": "x"}}], "moments": []},
    ],
    ids=[
        "other-track",
        "no-status",
        "ok-without-sections",
        "null-section",
        "partial-moment",
        "no-producer",
        "other-producer",
        "ok-empty-sections",
        "text-bound",
        "nan-bound",
        "ends-before-start",
        "numeric-label",
        "moment-without-time",
        "text-bars",
        "bool-start-bar",
        "numeric-dj-label",
        "text-ambient",
        "text-energy-score",
    ],
)
def test_a_current_schema_doc_that_cannot_be_served_is_quarantined(track: float, tmp_path: Path, bad: dict):
    path = fast_sidecar.fast_path(tmp_path, SID)
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = fast_sidecar.grid_digest(fast_sidecar.downbeats_of(_payload()))
    # Every field but the one under test is current, so only that one can be why it is quarantined.
    path.write_text(json.dumps({"schema": fast_sidecar.SCHEMA, "grid_digest": digest, "producer": "own-fast",
                                "producer_version": fast_sidecar.PRODUCER_VERSION,
                                "audio_rev": local_waveform.audio_revision(SID), **bad}), encoding="utf-8", newline="\n")  # fmt: skip
    out = _served(_payload(), tmp_path)
    assert out["own_phrases"]["status"] == "ok" and out["phrases_source"] == "own-fast"
    assert len(_quarantined(path)) == 1
    # Control: the recomputed doc itself passes the same check and is served from cache.
    stamp = path.stat().st_mtime_ns
    assert merge_own_phrases(_payload(), SID, tmp_path, source="rbx")["own_phrases"]["status"] == "ok"
    assert path.stat().st_mtime_ns == stamp and len(_quarantined(path)) == 1


def test_an_unreadable_cache_file_is_reported_not_overwritten(track: float, tmp_path: Path):
    path = fast_sidecar.fast_path(tmp_path, SID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("left by a failing disk", encoding="utf-8", newline="\n")
    path.chmod(0)
    try:
        try:
            path.read_bytes()
        except PermissionError:
            pass
        else:  # Windows ACLs, or a root runner: mode bits cannot deny this read
            pytest.skip("chmod(0) does not deny reads on this platform/user, so no read fault can be made")
        out = merge_own_phrases(_payload(), SID, tmp_path, source="rbx")
        assert out["own_phrases"]["status"] == "failed"
        assert out["own_phrases"]["reason"].startswith("cache_unreadable:")
        assert fast_sidecar._INFLIGHT == {}, "nothing may be queued to overwrite it"
    finally:
        path.chmod(0o644)
    assert path.read_text(encoding="utf-8") == "left by a failing disk"
    assert _quarantined(path) == []
    # Control: once readable it is ordinary corruption, quarantined and healed.
    assert _served(_payload(), tmp_path)["own_phrases"]["status"] == "ok"


@pytest.fixture
def announced() -> Iterator[list[str]]:
    """Track ids the REAL publish path (``apps.shared.events.publish``) delivered to the
    registered event hub as ``library.changed`` kind ``sections``; anything else it
    delivered is recorded as an unexpected tuple, so a wrong topic or payload fails."""
    from apps.shared import events

    seen: list[Any] = []

    class _Recorder:
        def publish(self, topic: str, payload: dict[str, Any]) -> None:
            ids = payload.get("ids")
            ok = topic == "library.changed" and payload.get("kind") == "sections"
            if ok and isinstance(ids, list) and len(ids) == 1:
                seen.extend(ids)
            else:
                seen.append(("UNEXPECTED", topic, payload))

    previous = events._Slot.hub
    events.set_hub(_Recorder())
    try:
        yield seen
    finally:
        events.set_hub(previous)


@pytest.fixture
def decoder_saturated():
    """Every decode admission permit taken, as when MAX_DECODE_WAITERS decodes are in flight."""
    deadline = time.monotonic() + 30  # an earlier test's background decode holds a permit until done
    while fast_sidecar._INFLIGHT and time.monotonic() < deadline:
        time.sleep(0.02)
    held = 0
    while local_waveform._DECODE_ADMISSION.acquire(blocking=False):
        held += 1
    assert held == local_waveform.MAX_DECODE_WAITERS
    try:
        yield
    finally:
        for _ in range(held):
            local_waveform._DECODE_ADMISSION.release()


def test_a_decoder_busy_past_its_retries_ends_failed_and_announced(
    track: float, tmp_path: Path, decoder_saturated: None, monkeypatch: pytest.MonkeyPatch, announced: list[str]
):
    """The SHIPPED retry schedule, every transition, through the production path (discussion_r4264058757).

    Nothing about the schedule is patched: BUSY_RETRIES and BUSY_RETRY_S keep
    their real values. Each real timer the production code arms is checked for
    its real delay, then its real callback is run at once instead of sleeping,
    which submits the next real attempt to the real executor. Only module state
    (the held-failure and retry tables) is reset so earlier tests cannot leak in."""
    monkeypatch.setattr(fast_sidecar, "_HELD_FAILURES", {})
    monkeypatch.setattr(fast_sidecar, "_RETRY_TIMERS", {})
    monkeypatch.setattr(fast_sidecar, "_RETRY_REVISIONS", {})
    downbeats = fast_sidecar.downbeats_of(_payload())
    key = (SID, fast_sidecar.grid_digest(downbeats))
    assert fast_sidecar.BUSY_RETRIES == 3 and fast_sidecar.BUSY_RETRY_S == 5.0, "control: the shipped schedule"
    first = fast_sidecar.compute(SID, downbeats, tmp_path, attempt=0)  # synchronous: no race on the check
    assert first["status"] == "pending" and first["reason"].startswith("decode_busy:")
    try:
        for n in range(1, fast_sidecar.BUSY_RETRIES + 1):
            timer = fast_sidecar._RETRY_TIMERS[key]
            assert timer.interval == fast_sidecar.BUSY_RETRY_S * n, f"retry {n} waits {5 * n}s, growing"
            assert announced == [], f"control: with retry {n} armed it stays pending, unannounced"
            timer.cancel()  # not slept through: its own callback runs now
            timer.function()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not (
                announced or fast_sidecar._RETRY_TIMERS.get(key) not in {None, timer}
            ):
                time.sleep(0.01)
    finally:
        for t in fast_sidecar._RETRY_TIMERS.values():
            t.cancel()
    assert announced == [SID], "the give-up after the third retry must reach a loaded deck"
    assert key not in fast_sidecar._RETRY_TIMERS, "no fourth retry"
    out = merge_own_phrases(_payload(), SID, tmp_path, source="rbx")
    assert out["own_phrases"]["status"] == "failed"
    assert out["own_phrases"]["reason"].startswith("decode_busy_retries_exhausted:")
    assert not fast_sidecar.fast_path(tmp_path, SID).exists(), "busy is not a fact about the track"
    assert fast_sidecar._INFLIGHT == {}, "a held give-up does not restart the cycle"


def test_a_held_give_up_expires_and_the_next_load_retries(
    track: float, tmp_path: Path, decoder_saturated: None, monkeypatch: pytest.MonkeyPatch
):
    """A real held give-up answers until a real 0.5 s hold runs out, then a load retries (discussion_r4264875954).

    Nothing seeded or patched: the hold is the production ``hold_s`` parameter
    (shipped default FAILURE_HOLD_S), the failure a real busy give-up."""
    monkeypatch.setattr(fast_sidecar, "_HELD_FAILURES", {})  # module state only: earlier tests' holds
    hold_s = 0.5
    downbeats = fast_sidecar.downbeats_of(_payload())
    gave_up = fast_sidecar.compute(SID, downbeats, tmp_path, attempt=fast_sidecar.BUSY_RETRIES)
    assert gave_up["status"] == "failed" and gave_up["reason"].startswith("decode_busy_retries_exhausted")
    held_at = time.monotonic()
    for _ in range(local_waveform.MAX_DECODE_WAITERS):
        local_waveform._DECODE_ADMISSION.release()  # the decoder is free again
    try:
        still = fast_sidecar.fast_sections(_payload(), SID, tmp_path, wait_s=0.0, hold_s=hold_s)
        assert time.monotonic() - held_at < hold_s, "control: asked inside the hold"
        assert still["status"] == "failed", "inside the hold the give-up answers, nothing restarts"
        assert fast_sidecar._INFLIGHT == {}
        time.sleep(hold_s + 0.1)  # the real hold runs out
        out = fast_sidecar.fast_sections(_payload(), SID, tmp_path, wait_s=0.0, hold_s=hold_s)
        assert out["status"] == "pending", "after the hold the next load retries, not the give-up"
        assert _served(_payload(), tmp_path)["own_phrases"]["status"] == "ok", "and the retry cuts"
    finally:
        for _ in range(local_waveform.MAX_DECODE_WAITERS):
            local_waveform._DECODE_ADMISSION.acquire()


def test_audio_that_cannot_be_decoded_now_is_held_not_written(
    track: float, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, announced: list[str]
):
    """An unmounted file is a fact about this moment: failed and announced, never cached on disk."""
    monkeypatch.setattr(fast_sidecar, "_HELD_FAILURES", {})
    audio = tmp_path / "arc.wav"
    away = tmp_path / "unmounted.wav"
    audio.rename(away)
    out = _served(_payload(), tmp_path)
    assert out["own_phrases"]["status"] == "failed"
    assert out["own_phrases"]["reason"].startswith("no_peaks:")
    assert announced == [SID]
    assert not fast_sidecar.fast_path(tmp_path, SID).exists(), "a written failure would match this grid forever"
    held = merge_own_phrases(_payload(), SID, tmp_path, source="rbx")
    assert held["own_phrases"]["status"] == "failed", "control: held, so a refetch does not restart the work"
    away.rename(audio)
    # The audio it failed on changed (it resolves again): inside the hold, the next load recuts.
    assert _served(_payload(), tmp_path)["own_phrases"]["status"] == "ok", "the drive is back: the next load heals"


def test_a_crashed_job_is_logged_held_and_announced_not_left_pending(
    track: float,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    announced: list[str],
):
    """A failure after `pending` was answered must reach the deck: failed, announced, traceback logged.

    The failure is a real one on the production path, not an injected one: the
    sidecar's temp file is a directory, so the write after segmenting raises."""
    monkeypatch.setattr(fast_sidecar, "_HELD_FAILURES", {})
    _served(_payload(), tmp_path)  # cache the peaks: the write is then the only failure
    fast_sidecar.fast_path(tmp_path, SID).with_suffix(".json.tmp").mkdir()
    downbeats = fast_sidecar.downbeats_of(_payload(offset_s=0.5, bars=63))
    fut = fast_sidecar._submit(SID, downbeats, tmp_path)
    crashed = fut.result(timeout=30)  # the crash is caught in the job and held, with its revision
    assert crashed["status"] == "failed" and crashed["reason"].startswith("error: IsADirectoryError")
    deadline = time.monotonic() + 5
    while SID not in announced and time.monotonic() < deadline:
        time.sleep(0.01)
    assert announced.count(SID) >= 1
    out = merge_own_phrases(_payload(offset_s=0.5, bars=63), SID, tmp_path, source="rbx")
    assert out["own_phrases"]["status"] == "failed"
    assert out["own_phrases"]["reason"].startswith("error: "), out["own_phrases"]["reason"]
    assert "own-fast sections job for" in caplog.text and "Traceback" in caplog.text


def test_a_burst_of_selections_cancels_the_oldest_waiting_jobs(
    track: float, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, announced: list[str]
):
    """Arrowing through undecoded rows must not queue a decode per row ahead of the one loaded."""
    gate = threading.Event()
    blocker = fast_sidecar._EXECUTOR.submit(gate.wait, 30)  # the one worker is busy
    try:
        futs = [
            fast_sidecar._submit(SID, fast_sidecar.downbeats_of(_payload(offset_s=k * 0.01, bars=60)), tmp_path)
            for k in range(fast_sidecar.MAX_QUEUED + 2)
        ]
        assert [f.cancelled() for f in futs] == [True, True] + [False] * fast_sidecar.MAX_QUEUED
        assert announced == [SID, SID], "a deck holding a cancelled job's pending answer must refetch"
        assert len(fast_sidecar._INFLIGHT) == fast_sidecar.MAX_QUEUED
    finally:
        gate.set()
        blocker.result(timeout=30)
    for f in futs[2:]:
        f.result(timeout=60)  # control: the newest ones still run to an answer


def test_the_peak_cache_is_read_before_the_worker_can_fill_it(track: float, tmp_path: Path):
    """The wait decision uses entry-time state: peaks cached after the check must not turn `pending` into a wait.

    Deterministic, nothing patched (discussion_r4263939420, issue #7450): the
    request thread's profiler parks on the CALL of the real ``_submit``, then
    this test decodes the real peaks into the real cache, then ``_submit``
    runs. A request that already read the cache (before ``_submit``) answers
    ``pending`` at once; one that reads it after submitting would see it
    cached and wait for the cut.
    """
    assert local_waveform.peaks_entry_current(SID) is not True, "control: no peaks cached yet"
    at_submit, release = threading.Event(), threading.Event()
    order: list[str] = []
    out: dict[str, Any] = {}
    peaks_code = fast_sidecar._peaks_cached.__code__
    submit_code = fast_sidecar._submit.__code__

    def request() -> None:
        parked = False

        def profiler(frame, event, arg):
            nonlocal parked
            if event != "call":
                return
            if frame.f_code is peaks_code:
                order.append("peaks")
            elif frame.f_code is submit_code and not parked:
                parked = True
                order.append("submit")
                at_submit.set()
                release.wait(timeout=60)

        previous = sys.getprofile()
        sys.setprofile(profiler)
        try:
            out.update(fast_sidecar.fast_sections(_payload(), SID, tmp_path, wait_s=30.0))
        finally:
            sys.setprofile(previous)

    req = threading.Thread(target=request, daemon=True)
    req.start()
    assert at_submit.wait(timeout=60), "control: the request entered the real _submit"
    local_waveform.ensure_local_peaks(SID)
    assert local_waveform.peaks_entry_current(SID) is True, "control: the real peaks are now cached"
    release.set()
    req.join(timeout=60)
    assert not req.is_alive(), "control: the request finished"
    assert order == ["peaks", "submit"], "control: the entry-time peak read ran before _submit"
    assert out["status"] == "pending", "read at entry: uncached, so no wait, whatever the cache holds later"
    _served(_payload(), tmp_path)


def test_stale_cached_peaks_do_not_hold_the_load(track: float, tmp_path: Path):
    """Peaks cached for the OLD audio are not cached: the request answers pending at once."""
    _served(_payload(), tmp_path)
    assert local_waveform.peaks_entry_current(SID) is True  # control: current entry reads current
    audio = tmp_path / "arc.wav"
    _write_arc(audio)  # replaced: new bytes, new ctime and mtime
    os.utime(audio, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
    assert local_waveform._entry_path(SID).is_file(), "the old entry is still on disk"
    assert local_waveform.peaks_entry_current(SID) is False
    started = time.monotonic()
    out = merge_own_phrases(_payload(offset_s=0.5, bars=63), SID, tmp_path, source="rbx")
    elapsed = time.monotonic() - started
    assert out["own_phrases"]["status"] == "pending"
    assert elapsed < fast_sidecar.INLINE_WAIT_S * 0.8, f"waited {elapsed:.3f}s on a decode"


def test_a_finished_job_leaves_no_inflight_entry(track: float, tmp_path: Path):
    """Each finished job removes its own in-flight entry, so a later grid change resubmits."""
    _served(_payload(), tmp_path)
    fut = fast_sidecar._submit(SID, fast_sidecar.downbeats_of(_payload()), tmp_path)
    fut.result(timeout=30)
    fut.add_done_callback(lambda _f: None)  # already done: runs inline
    deadline = time.monotonic() + 5
    while fast_sidecar._INFLIGHT and time.monotonic() < deadline:
        time.sleep(0.01)
    assert fast_sidecar._INFLIGHT == {}


def test_replaced_audio_recuts_cached_sections_on_the_same_grid(track: float, tmp_path: Path):
    """Sections cut from the OLD bytes are not served after the audio is replaced under the same id and grid."""
    _served(_payload(), tmp_path)
    again = merge_own_phrases(_payload(), SID, tmp_path, source="rbx")
    assert again["own_phrases"]["status"] == "ok", "control: same audio, same grid serves the cache"
    stamp = fast_sidecar.fast_path(tmp_path, SID).stat().st_mtime_ns
    audio = tmp_path / "arc.wav"
    _write_arc(audio)  # replaced: new ctime and mtime
    os.utime(audio, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
    out = merge_own_phrases(_payload(), SID, tmp_path, source="rbx")
    assert out["own_phrases"]["status"] == "pending", "a replaced file must not be served the old cut"
    healed = _served(_payload(), tmp_path)
    assert healed["own_phrases"]["status"] == "ok"
    assert fast_sidecar.fast_path(tmp_path, SID).stat().st_mtime_ns != stamp, "recut and rewritten"


def test_unresolvable_audio_does_not_serve_the_cached_cut(track: float, tmp_path: Path):
    """A cached cut is only served for audio that is there to compare: unknown is a miss, never a hit."""
    _served(_payload(), tmp_path)
    assert merge_own_phrases(_payload(), SID, tmp_path, source="rbx")["own_phrases"]["status"] == "ok"  # control
    (tmp_path / "arc.wav").unlink()
    out = _served(_payload(), tmp_path)
    assert out["own_phrases"]["status"] == "failed", "the old cut must not mask a missing file"
    assert out["phrases_source"] == "none"


def test_busy_retries_are_coalesced_and_bounded(
    track: float, tmp_path: Path, decoder_saturated: None, monkeypatch: pytest.MonkeyPatch, announced: list[str]
):
    """A scroll through busy rows holds at most MAX_RETRY_TIMERS sleeping retries; the oldest is dropped and announced."""
    # Shipped cap and delay (discussion_r4264186294): only module state is reset.
    # The first retry waits the shipped 5 s; every timer is cancelled in cleanup.
    monkeypatch.setattr(fast_sidecar, "_RETRY_TIMERS", {})
    monkeypatch.setattr(fast_sidecar, "_RETRY_REVISIONS", {})
    monkeypatch.setattr(fast_sidecar, "_HELD_FAILURES", {})
    cap = fast_sidecar.MAX_RETRY_TIMERS
    assert cap == fast_sidecar.MAX_QUEUED == 4, "control: the shipped cap"
    grids = [fast_sidecar.downbeats_of(_payload(offset_s=k * 0.5, bars=60)) for k in range(cap + 3)]
    try:
        first = fast_sidecar.compute(SID, grids[0], tmp_path)
        again = fast_sidecar.compute(SID, grids[0], tmp_path)
        assert first["status"] == again["status"] == "pending"
        assert len(fast_sidecar._RETRY_TIMERS) == 1, "the same track and grid retries once, not twice"
        assert announced == [], "control: under the cap nothing is dropped"
        for downbeats in grids[1:]:
            fast_sidecar.compute(SID, downbeats, tmp_path)
        assert len(fast_sidecar._RETRY_TIMERS) == cap, "bounded at MAX_RETRY_TIMERS"
        assert announced == [SID, SID, SID], "each dropped retry is announced so its deck asks again"
        # A dropped retry is a held failure, so the deck's refetch gets an answer, not a fresh cycle.
        refetch = fast_sidecar.fast_sections(_payload(offset_s=0.0, bars=60), SID, tmp_path)
        assert refetch["status"] == "failed" and refetch["reason"].startswith("decode_busy_displaced")
        assert len(fast_sidecar._RETRY_TIMERS) == cap, "the refetch did not start another retry"
    finally:
        for timer in fast_sidecar._RETRY_TIMERS.values():
            timer.cancel()


def test_a_finished_cut_cancels_the_sleeping_retry(
    track: float, tmp_path: Path, decoder_saturated: None, monkeypatch: pytest.MonkeyPatch, announced: list[str]
):
    """A retry left sleeping by a busy attempt must not cut and announce again once another attempt succeeded.

    Deterministic: the shipped first-retry delay is asserted as a constant control, then the
    sleeping retry is held far past the cut's own duration. The assertion being made is
    "the cut cancelled the retry before it fired", and how long the host needs to decode +
    segment is not this test's business — a loaded host can take longer than the 5 s
    production timer, which made the old wall-clock read of the margin fail under load.
    """
    # The shipped 5 s timer (discussion_r4264558053): the constant is the control, only
    # module state is reset, and the held interval keeps the race off the wall clock.
    assert fast_sidecar.BUSY_RETRY_S == 5.0, "control: the shipped first-retry delay"
    assert fast_sidecar.BUSY_RETRIES == 3, "control: the shipped retry schedule length"
    monkeypatch.setattr(fast_sidecar, "BUSY_RETRY_S", 600.0)  # held: the cut's duration belongs to the host
    monkeypatch.setattr(fast_sidecar, "_RETRY_TIMERS", {})
    downbeats = fast_sidecar.downbeats_of(_payload())
    key = (SID, fast_sidecar.grid_digest(downbeats))
    try:
        assert fast_sidecar.compute(SID, downbeats, tmp_path)["status"] == "pending"
        timer = fast_sidecar._RETRY_TIMERS[key]
        assert timer.is_alive(), "control: the busy attempt left a retry sleeping"
        assert timer.interval == fast_sidecar.BUSY_RETRY_S == 600.0, "control: armed from the shipped constant"
        for _ in range(local_waveform.MAX_DECODE_WAITERS):
            local_waveform._DECODE_ADMISSION.release()
        try:
            assert fast_sidecar.compute(SID, downbeats, tmp_path)["status"] == "ok"
        finally:
            for _ in range(local_waveform.MAX_DECODE_WAITERS):
                local_waveform._DECODE_ADMISSION.acquire()
        assert key not in fast_sidecar._RETRY_TIMERS
        timer.join(timeout=5)
        assert not timer.is_alive() and timer.finished.is_set(), "cancelled, not left to fire"
        assert announced == [SID], "announced once, by the attempt that finished"
    finally:
        for t in fast_sidecar._RETRY_TIMERS.values():
            t.cancel()


def test_audio_revision_moves_with_the_peaks_producer(track: float):
    """New peaks from the same file (another real producer configuration) recut the sections too.

    Nothing patched: ``DecodeProfile`` is the production value that decides
    what peaks mean, so a DIFFERENT real profile is constructed and passed,
    as test_cache_version does for the peak cache itself."""
    before = local_waveform.audio_revision(SID)
    assert before is not None and local_waveform.audio_revision(SID) == before, "control: stable when nothing moved"
    assert local_waveform.audio_revision(SID, decode.PROFILE) == before, "control: the shipped profile is the default"
    other = decode.DecodeProfile(crossover_high_hz=5_000)
    assert local_waveform.peaks_version(other) != local_waveform.peaks_version(), "control: a real, different producer"
    assert local_waveform.audio_revision(SID, other) not in {None, before}


def test_quarantining_an_already_quarantined_file_is_a_no_op(tmp_path: Path):
    """Two requests can read the same corrupt file; the second finds it already moved."""
    fast_sidecar._quarantine(tmp_path / "gone.json", "test")
    assert list(tmp_path.iterdir()) == []


def test_a_job_cutting_audio_replaced_mid_run_is_not_served(
    track: float, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A REAL cut whose audio is replaced while it runs is never served; the next load recuts the new bytes.

    Deterministic: the test holds the real per-track decode lock, and waits
    until the real worker has taken its decode admission permit, which
    ensure_local_peaks does only after compute() has read the old audio
    revision. The swap therefore always lands mid-run."""
    monkeypatch.setattr(fast_sidecar, "_HELD_FAILURES", {})  # an earlier test's held give-up is not this one's
    deadline = time.monotonic() + 60  # an earlier test's background decode holds a permit until done
    while fast_sidecar._INFLIGHT and time.monotonic() < deadline:
        time.sleep(0.02)
    admission = local_waveform._DECODE_ADMISSION
    assert admission._value == local_waveform.MAX_DECODE_WAITERS, "control: no other decode in flight"
    audio = tmp_path / "arc.wav"
    old_rev = local_waveform.audio_revision(SID)
    key = (SID, fast_sidecar.grid_digest(fast_sidecar.downbeats_of(_payload())))
    with local_waveform._track_lock(SID):
        assert fast_sidecar.fast_sections(_payload(), SID, tmp_path, wait_s=0.0)["status"] == "pending"
        job = fast_sidecar._INFLIGHT[key]
        deadline = time.monotonic() + 60
        while admission._value == local_waveform.MAX_DECODE_WAITERS and time.monotonic() < deadline:
            time.sleep(0.005)
        assert admission._value < local_waveform.MAX_DECODE_WAITERS, "control: the real cut read the revision"
        assert not job.done(), "control: the real cut is parked on the held lock"
        _write_arc(audio)  # replaced mid-run: new bytes, ctime and mtime
        os.utime(audio, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
        new_rev = local_waveform.audio_revision(SID)
        assert new_rev != old_rev
    stale = job.result(timeout=60)
    assert stale["status"] == "ok" and stale["audio_rev"] == old_rev, "control: the real job cut the OLD revision"
    on_disk = json.loads(fast_sidecar.fast_path(tmp_path, SID).read_text(encoding="utf-8"))
    assert on_disk["audio_rev"] == old_rev, "control: the stale cut is the sidecar a load would find"
    out = fast_sidecar.fast_sections(_payload(), SID, tmp_path, wait_s=0.0)
    assert out.get("audio_rev") != old_rev, "the old bytes' cut must not be served"
    assert out["status"] in {"pending", "ok"}
    deadline = time.monotonic() + 60
    while fast_sidecar._INFLIGHT and time.monotonic() < deadline:
        time.sleep(0.02)
    healed = merge_own_phrases(_payload(), SID, tmp_path, source="rbx")
    assert healed["own_phrases"]["status"] == "ok"
    rewritten = json.loads(fast_sidecar.fast_path(tmp_path, SID).read_text(encoding="utf-8"))
    assert rewritten["audio_rev"] == new_rev != old_rev, "recut from the new bytes"


def test_a_terminal_decode_failure_cancels_the_sleeping_retry(
    track: float, tmp_path: Path, decoder_saturated: None, monkeypatch: pytest.MonkeyPatch
):
    """A busy attempt's sleeping retry must not cut and announce again after another attempt failed for good."""
    # The shipped 5 s timer (discussion_r4264558053): only module state is reset.
    monkeypatch.setattr(fast_sidecar, "_RETRY_TIMERS", {})
    monkeypatch.setattr(fast_sidecar, "_HELD_FAILURES", {})
    downbeats = fast_sidecar.downbeats_of(_payload())
    key = (SID, fast_sidecar.grid_digest(downbeats))
    try:
        assert fast_sidecar.compute(SID, downbeats, tmp_path)["status"] == "pending"
        armed = time.monotonic()
        timer = fast_sidecar._RETRY_TIMERS[key]
        assert timer.is_alive(), "control: the busy attempt left a retry sleeping"
        assert timer.interval == fast_sidecar.BUSY_RETRY_S == 5.0, "control: the shipped first-retry delay"
        (tmp_path / "arc.wav").rename(tmp_path / "unmounted.wav")
        for _ in range(local_waveform.MAX_DECODE_WAITERS):
            local_waveform._DECODE_ADMISSION.release()
        try:
            failed = fast_sidecar.compute(SID, downbeats, tmp_path)
        finally:
            for _ in range(local_waveform.MAX_DECODE_WAITERS):
                local_waveform._DECODE_ADMISSION.acquire()
        assert failed["status"] == "failed" and failed["reason"].startswith("no_peaks:")
        assert key not in fast_sidecar._RETRY_TIMERS
        assert time.monotonic() - armed < timer.interval, "control: cancelled before the real timer was due"
        timer.join(timeout=5)
        assert not timer.is_alive() and timer.finished.is_set(), "cancelled, not left to fire"
    finally:
        for t in fast_sidecar._RETRY_TIMERS.values():
            t.cancel()


def test_a_failure_from_audio_replaced_mid_attempt_is_held_against_the_old_bytes(
    track: float, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The held failure keeps the revision the attempt read BEFORE its decode, so the
    replacement file is recut instead of inheriting the old attempt's failure.

    Deterministic: the test holds the real per-track decode lock, so the real
    worker has read the old revision and is parked inside ensure_local_peaks
    when the file is replaced with bytes that cannot be decoded."""
    monkeypatch.setattr(fast_sidecar, "_HELD_FAILURES", {})
    audio = tmp_path / "arc.wav"
    old_rev = local_waveform.audio_revision(SID)
    key = (SID, fast_sidecar.grid_digest(fast_sidecar.downbeats_of(_payload())))
    with local_waveform._track_lock(SID):
        assert fast_sidecar.fast_sections(_payload(), SID, tmp_path, wait_s=0.0)["status"] == "pending"
        job = fast_sidecar._INFLIGHT[key]
        deadline = time.monotonic() + 5
        while not job.running() and time.monotonic() < deadline:
            time.sleep(0.005)
        time.sleep(0.2)  # compute() reads the revision first thing, then parks on this lock
        assert job.running() and not job.done(), "control: the real attempt is parked on the held lock"
        audio.write_bytes(b"not audio at all")
        os.utime(audio, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
        new_rev = local_waveform.audio_revision(SID)
        assert new_rev != old_rev
    failed = job.result(timeout=30)
    assert failed["status"] == "failed", "control: the old attempt failed on the replaced bytes"
    assert fast_sidecar._HELD_FAILURES[key][2] == old_rev, "held against the bytes the attempt read"
    assert fast_sidecar._held_failure(key) is None, "the replacement is not answered with the old failure"


def test_a_retry_displaced_after_its_audio_was_replaced_is_held_against_the_old_bytes(
    track: float, tmp_path: Path, decoder_saturated: None, monkeypatch: pytest.MonkeyPatch, announced: list[str]
):
    """A busy retry dropped at the cap is held against the audio its attempt read (discussion_r4263785777).

    Deterministic: the real decoder is saturated, so the first cut sleeps on a
    retry; the audio is then replaced, and two more grids push it past the cap."""
    monkeypatch.setattr(fast_sidecar, "_RETRY_TIMERS", {})
    monkeypatch.setattr(fast_sidecar, "_RETRY_REVISIONS", {})
    monkeypatch.setattr(fast_sidecar, "_HELD_FAILURES", {})
    grids = [
        fast_sidecar.downbeats_of(_payload(offset_s=k * 0.5, bars=60)) for k in range(fast_sidecar.MAX_RETRY_TIMERS + 1)
    ]
    key = (SID, fast_sidecar.grid_digest(grids[0]))
    old_rev = local_waveform.audio_revision(SID)
    try:
        assert fast_sidecar.compute(SID, grids[0], tmp_path)["status"] == "pending"
        assert key in fast_sidecar._RETRY_TIMERS, "control: the busy cut sleeps on a retry"
        audio = tmp_path / "arc.wav"
        _write_arc(audio)  # replaced while the retry sleeps
        os.utime(audio, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
        assert local_waveform.audio_revision(SID) != old_rev, "control: the audio really moved"
        for downbeats in grids[1:]:
            fast_sidecar.compute(SID, downbeats, tmp_path)
        assert key not in fast_sidecar._RETRY_TIMERS, "control: the first retry was displaced"
        assert fast_sidecar._HELD_FAILURES[key][2] == old_rev, "held against the bytes the attempt read"
        assert fast_sidecar._held_failure(key) is None, "the replaced audio is recut, not refused"
    finally:
        for timer in fast_sidecar._RETRY_TIMERS.values():
            timer.cancel()


def test_a_worker_crash_after_its_audio_was_replaced_is_held_against_the_old_bytes(
    track: float, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A cut that crashes is held against the revision it read before decoding (discussion_r4263939431).

    Real crash: the sidecar directory is a FILE, so the real write fails. The
    test holds the real per-track lock, so the job parks after reading the
    revision; the audio is replaced; then the job runs on and crashes."""
    monkeypatch.setattr(fast_sidecar, "_HELD_FAILURES", {})
    deadline = time.monotonic() + 60
    while fast_sidecar._INFLIGHT and time.monotonic() < deadline:
        time.sleep(0.02)
    admission = local_waveform._DECODE_ADMISSION
    assert admission._value == local_waveform.MAX_DECODE_WAITERS, "control: no other decode in flight"
    not_a_dir = tmp_path / "not-a-dir"
    not_a_dir.write_text("x", encoding="utf-8")
    downbeats = fast_sidecar.downbeats_of(_payload())
    key = (SID, fast_sidecar.grid_digest(downbeats))
    old_rev = local_waveform.audio_revision(SID)
    out: dict[str, Any] = {}
    with local_waveform._track_lock(SID):
        job = threading.Thread(target=lambda: out.update(fast_sidecar.compute(SID, downbeats, not_a_dir)), daemon=True)
        job.start()
        deadline = time.monotonic() + 60
        while admission._value == local_waveform.MAX_DECODE_WAITERS and time.monotonic() < deadline:
            time.sleep(0.005)
        assert admission._value < local_waveform.MAX_DECODE_WAITERS, "control: the job read the revision"
        audio = tmp_path / "arc.wav"
        _write_arc(audio)
        os.utime(audio, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
        assert local_waveform.audio_revision(SID) != old_rev, "control: the audio really moved"
    job.join(timeout=60)
    assert out["status"] == "failed" and out["reason"].startswith("error:"), "control: a real crash"
    assert fast_sidecar._HELD_FAILURES[key][2] == old_rev, "held against the bytes the attempt read"
    assert fast_sidecar._held_failure(key) is None, "the replaced audio is recut, not refused"


def test_a_share_request_starts_no_own_fast_work(track: float, tmp_path: Path):
    """A share listener may hear another copy: own-fast never decodes or caches for it (discussion_r4264419246).

    Real path, nothing patched: the share request leaves no job, no waveform
    cache entry and no sidecar; the same request on the local audience does."""
    deadline = time.monotonic() + 60
    while fast_sidecar._INFLIGHT and time.monotonic() < deadline:
        time.sleep(0.02)
    entry = local_waveform._entry_path(SID)
    entry.unlink(missing_ok=True)
    out = merge_own_phrases(_payload(), SID, tmp_path, source="rbx", share=True)
    assert out["phrases_source"] == "none" and out["phrases"] == []
    assert out["own_phrases"]["status"] == "failed" and out["own_phrases"]["reason"].startswith("share_audience")
    assert fast_sidecar._INFLIGHT == {}, "no own-fast job for a share request"
    assert not entry.exists(), "the share request decoded nothing into the waveform cache"
    assert not fast_sidecar.fast_path(tmp_path, SID).exists()
    local = _served(_payload(), tmp_path)  # control: the local audience does cut
    assert local["own_phrases"]["status"] == "ok" and entry.exists()
