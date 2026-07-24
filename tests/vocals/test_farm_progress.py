"""Regression tests for the farm's progress stream, reducer and publish queue.

These cover the parts of the async rework that are verifiable WITHOUT Modal
spend: the event contract, the arithmetic every reader depends on, and the
central claim that queued transfers overlap GPU work instead of serialising
with it.

Single-line intent, in the repo's regression style:
  - if an unknown event type is accepted silently then the stream contract is broken
  - if in-flight counts do not settle to zero after a run then the reducer is broken
  - if a stage failure is charged to the wrong stage then in-flight shows a phantom backlog
  - if PublishQueue does not overlap transfer with the result loop then the async rework is broken
  - if a bounded queue stops applying backpressure then a slow uplink becomes an OOM
  - if the bifrost2 dir parsers mis-read sizes then the archive verify is meaningless
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from scripts.b2listing import remote_dirs, remote_sizes
from scripts.farm_progress import (
    EVENT_TYPES,
    FarmState,
    ProgressLog,
    read_events,
    reduce_events,
    snapshot_text,
)

L4_USD_PER_S = 0.80 / 3600


def _run_events(tracks: int = 6) -> list[dict]:
    """A whole synthetic run: one gpu failure, one stems failure, rest clean."""
    events: list[dict] = [
        {"event": "run_start", "ts": 0.0, "run_id": "t", "tracks": tracks,
         "preset": "htdemucs-ov0.1", "dest": "r2", "max_containers": 10,
         "source_bytes": tracks * 9_000_000},
    ]
    clock = 0.0
    for index in range(tracks):
        stable_id = f"trk{index}"
        clock += 1.0
        events.append({"event": "upload", "ts": clock, "stable_id": stable_id,
                       "bytes": 9_000_000, "s": 0.3})
        if index == 1:
            events.append({"event": "failed", "ts": clock, "stable_id": stable_id,
                           "stage": "gpu", "error": "ffprobe blew up"})
            continue
        events.append({"event": "gpu", "ts": clock, "stable_id": stable_id,
                       "separate_s": 20.0, "container_s": 30.0,
                       "coverage_pct": 50.0, "regions": 12})
        events.append({"event": "stems_queued", "ts": clock,
                       "stable_id": stable_id, "bytes": 74_000_000})
        if index == 3:
            events.append({"event": "failed", "ts": clock, "stable_id": stable_id,
                           "stage": "stems", "error": "503 SlowDown"})
            continue
        events.append({"event": "stems", "ts": clock, "stable_id": stable_id,
                       "bytes": 74_000_000, "s": 1.0, "dest": "r2"})
        events.append({"event": "written", "ts": clock, "stable_id": stable_id,
                       "regions": 12})
    events.append({"event": "run_end", "ts": clock, "written": tracks - 2,
                   "failed": 2, "wall_s": clock, "container_s": 30.0 * (tracks - 1),
                   "usd": 0.1})
    return events


# ----- stream contract --------------------------------------------------------

def test_unknown_event_is_rejected_on_emit(tmp_path: Path) -> None:
    log = ProgressLog(tmp_path / "run.jsonl", "run")
    with pytest.raises(ValueError, match="unknown farm event"):
        log.emit("uplaod", stable_id="x")  # typo, must not be silently written
    log.close()


def test_unknown_event_is_rejected_on_reduce() -> None:
    state = FarmState()
    with pytest.raises(ValueError, match="unknown farm event"):
        state.apply({"event": "not_a_real_event"})


def test_emitted_lines_round_trip_whole(tmp_path: Path) -> None:
    """A reader must never see a fragment; every line is complete JSON."""
    path = tmp_path / "run.jsonl"
    with ProgressLog(path, "run") as log:
        for index in range(50):
            log.emit("written", stable_id=f"t{index}", regions=index)
    events = read_events(path)
    assert len(events) == 50
    assert [e["regions"] for e in events] == list(range(50))
    assert all(e["run_id"] == "run" for e in events)


def test_concurrent_emitters_do_not_interleave(tmp_path: Path) -> None:
    """The queue drains on worker threads while the main loop emits too."""
    import threading

    path = tmp_path / "run.jsonl"
    log = ProgressLog(path, "run")

    def spam(tag: str) -> None:
        for index in range(100):
            log.emit("written", stable_id=f"{tag}{index}", regions=index)

    threads = [threading.Thread(target=spam, args=(t,)) for t in "abcd"]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    log.close()
    assert len(read_events(path)) == 400  # a torn line would fail to parse


# ----- reducer arithmetic -----------------------------------------------------

def test_in_flight_settles_to_zero_after_a_complete_run() -> None:
    state = reduce_events(_run_events())
    for stage in ("upload", "gpu", "stems", "written"):
        assert state.in_flight(stage) == 0, f"{stage} left a phantom backlog"
    assert state.settled == state.tracks


def test_failures_are_charged_to_their_own_stage() -> None:
    state = reduce_events(_run_events())
    assert state.failed == 2
    assert state.failed_at["gpu"] == 1
    assert state.failed_at["stems"] == 1
    assert state.failed_at["upload"] == 0


def test_mid_run_in_flight_is_positive_where_work_is_pending() -> None:
    """Truncate the run part-way: the stage holding work must show it."""
    events = _run_events()
    cut = [e for e in events if e["event"] in ("run_start", "upload")]
    state = reduce_events(cut)
    assert state.in_flight("gpu") == state.done["upload"]
    assert state.in_flight("upload") == 0  # all uploads emitted


def test_binding_flips_to_transfer_when_local_pushes_dominate() -> None:
    """The whole point of the dashboard verdict: a slow uplink must show as
    TRANSFER, because that is the condition --stems-dest r2 exists to fix."""
    state = FarmState()
    state.apply({"event": "run_start", "ts": 0, "tracks": 2, "max_containers": 10})
    state.apply({"event": "gpu", "ts": 1, "container_s": 30.0})
    state.apply({"event": "stems", "ts": 2, "bytes": 74_000_000, "s": 100.0})
    # 30s of GPU across 10 containers = 3s of wall; 100s of pushing dwarfs it.
    assert state.binding == "TRANSFER"

    fast = FarmState()
    fast.apply({"event": "run_start", "ts": 0, "tracks": 2, "max_containers": 10})
    fast.apply({"event": "gpu", "ts": 1, "container_s": 300.0})
    fast.apply({"event": "stems", "ts": 2, "bytes": 74_000_000, "s": 1.0})
    assert fast.binding == "GPU"


def test_rates_use_transfer_seconds_not_wall() -> None:
    state = reduce_events(_run_events())
    # 4 clean tracks x 74 MB over 1.0s each = 74 MB/s, regardless of wall.
    assert state.mb_per_s("stems") == pytest.approx(74.0, rel=0.01)


def test_snapshot_text_is_agent_readable() -> None:
    text = snapshot_text(reduce_events(_run_events()), L4_USD_PER_S)
    assert "binding=" in text
    assert "tracks/min" in text
    for stage in ("upload", "gpu", "stems", "written"):
        assert stage in text


def test_every_event_the_farm_emits_is_a_known_type() -> None:
    """Guards the farm/reducer contract from drifting apart silently."""
    emitted = {e["event"] for e in _run_events()}
    assert emitted <= EVENT_TYPES


# ----- the async claim --------------------------------------------------------

class _SlowFakeStore:
    """Stands in for bifrost2 at a fixed, known transfer cost."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.saved: list[str] = []

    def save(self, local_path: str, remote_subdir: str = "") -> str:
        time.sleep(self.seconds)
        self.saved.append(local_path)
        return remote_subdir


def test_publish_queue_overlaps_transfer_with_the_result_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE regression for the whole rework.

    Inline publishing costs (gpu_gap + transfer) per track. Queued publishing
    costs about max(gpu_gap, transfer/workers). With the numbers below the
    inline shape cannot finish faster than 1.2s while the queued shape should
    land near 0.6s, so the assertion has a wide margin and still only passes
    if the transfer genuinely overlapped.
    """
    import scripts.modal_vocal_farm as farm

    tracks = 6
    gpu_gap = 0.10      # time between results arriving from Modal
    transfer = 0.10     # time one bundle takes to push
    workers = 2

    pushed: list[str] = []

    def fake_publish(stable_id, audio_path, result, preset, store):
        time.sleep(transfer)
        pushed.append(stable_id)
        return 74_000_000, transfer

    monkeypatch.setattr(farm, "_publish_stems", fake_publish)

    preset = farm.PRESETS["htdemucs-ov0.1"]
    queue = farm.PublishQueue(_SlowFakeStore(transfer), preset, workers, depth=3)

    started = time.perf_counter()
    for index in range(tracks):
        time.sleep(gpu_gap)  # results trickle in from the GPUs
        queue.submit(f"trk{index}", Path(f"/nope/{index}.mp3"), {"stems": {}})
    queue.drain()
    elapsed = time.perf_counter() - started

    serial = tracks * (gpu_gap + transfer)   # 1.20s, the old inline shape
    assert len(pushed) == tracks
    assert elapsed < serial * 0.85, (
        f"queued publishing took {elapsed:.2f}s; the inline shape it replaces "
        f"would take about {serial:.2f}s, so no overlap happened"
    )


def test_publish_queue_reports_failures_without_losing_the_track(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.modal_vocal_farm as farm

    def boom(stable_id, audio_path, result, preset, store):
        raise RuntimeError("bifrost2 said no")

    monkeypatch.setattr(farm, "_publish_stems", boom)
    preset = farm.PRESETS["htdemucs-ov0.1"]
    queue = farm.PublishQueue(_SlowFakeStore(0.0), preset, workers=1, depth=2)
    queue.submit("trk0", Path("/nope/0.mp3"), {"stems": {}})
    queue.drain()

    outcome = queue.outcomes.get_nowait()
    assert outcome.stable_id == "trk0"
    assert "bifrost2 said no" in outcome.error
    assert outcome.bytes_pushed == 0


def test_publish_queue_is_bounded_so_ram_cannot_run_away(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full queue must BLOCK the submitter. Unbounded here would convert a
    slow uplink into unbounded memory and then an OOM that loses the run."""
    import scripts.modal_vocal_farm as farm

    release = 0.15

    def slow(stable_id, audio_path, result, preset, store):
        time.sleep(release)
        return 1, release

    monkeypatch.setattr(farm, "_publish_stems", slow)
    preset = farm.PRESETS["htdemucs-ov0.1"]
    queue = farm.PublishQueue(_SlowFakeStore(0.0), preset, workers=1, depth=1)

    started = time.perf_counter()
    for index in range(4):
        queue.submit(f"trk{index}", Path(f"/nope/{index}.mp3"), {"stems": {}})
    submit_elapsed = time.perf_counter() - started
    queue.drain()
    assert submit_elapsed > release, (
        "submitting 4 bundles into a depth-1 queue returned instantly, so the "
        "queue is not applying backpressure"
    )


# ----- bifrost2 listing parsers ------------------------------------------------

_LISTING = """ Volume in drive D is Data
 Directory of D:\\asset-store\\stems\\htdemucs-ov0.1\\abc123

24/07/2026  10:08    <DIR>          .
24/07/2026  10:08    <DIR>          ..
24/07/2026  10:08        74,182,344 vocals.flac
24/07/2026  10:08         1,204 manifest.json
               2 File(s)     74,183,548 bytes
"""


def test_remote_sizes_reads_comma_grouped_bytes() -> None:
    sizes = remote_sizes(_LISTING)
    assert sizes == {"vocals.flac": 74_182_344, "manifest.json": 1_204}


def test_remote_dirs_drops_dot_entries() -> None:
    listing = _LISTING.replace("74,182,344 vocals.flac", "<DIR>          bundle1")
    assert remote_dirs(listing) == {"bundle1"}


def test_sub_second_window_reports_no_rate_rather_than_a_fake_one() -> None:
    """A burst of events in one instant must not read as a five-figure rate."""
    events = [e for e in _run_events()]
    for event in events:
        event["ts"] = 0.0  # everything lands in the same instant
    state = reduce_events(events)
    assert state.tracks_per_min == 0.0
