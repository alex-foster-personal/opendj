"""Regression tests for the farm's read-ahead input feeder.

The feeder is the fan-out. Modal's sync ``.starmap`` pulls its input iterator
inline on the event-loop thread, so every second spent producing an input is a
second in which nothing uploads, dispatches or returns. These cover the
properties the farm depends on, none of which need Modal or a GPU.

Single-line intent, in the repo's regression style:
  - if read_ahead does not yield in input order then every track is signed with another track's file
  - if reads do not actually overlap then the fix bought nothing and concurrency stays at 1
  - if the in-flight window exceeds depth then memory is unbounded by construction
  - if the byte budget is not enforced then a batch of large files OOMs the Mac
  - if a file larger than the whole budget stalls then the feeder deadlocks on one track
  - if the stat is taken before the read then iCloud re-materialisation poisons every signature
  - if a read error is swallowed then a corrupt path silently drops a track
  - if abandoning the generator hangs then a mid-run abort never terminates
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from apps.vocals.prefetch import ReadyFile, read_ahead


def _write(directory: Path, name: str, size: int) -> Path:
    path = directory / name
    path.write_bytes(bytes([ord(name[0]) % 256]) * size)
    return path


def test_yields_in_input_order(tmp_path: Path) -> None:
    """if read_ahead does not yield in input order then signatures are crossed"""
    paths = [_write(tmp_path, f"{i:03d}.bin", 64) for i in range(25)]
    got = [ready.path for ready in read_ahead(paths, depth=8, workers=4)]
    assert got == paths


def test_content_matches_each_path(tmp_path: Path) -> None:
    """if bytes are paired with the wrong path then tracks are separated as each other"""
    paths = [_write(tmp_path, f"{chr(97 + i)}.bin", 32 + i) for i in range(10)]
    for ready in read_ahead(paths, depth=4, workers=3):
        assert ready.data == ready.path.read_bytes()
        assert ready.stat.st_size == len(ready.data)


def test_reads_actually_overlap(tmp_path: Path) -> None:
    """if reads do not overlap then the fix bought nothing"""
    # A real slow read, not a mocked one: patch nothing, just make the file
    # slow to produce by putting the delay in a subclassed Path.read_bytes.
    delay_s = 0.15
    count = 8

    class SlowPath(type(tmp_path)):  # type: ignore[misc]
        def read_bytes(self) -> bytes:
            time.sleep(delay_s)
            return super().read_bytes()

    paths = [_write(tmp_path, f"s{i}.bin", 16) for i in range(count)]
    slow = [SlowPath(str(p)) for p in paths]

    started = time.perf_counter()
    consumed = list(read_ahead(slow, depth=count, workers=count))
    elapsed = time.perf_counter() - started

    assert len(consumed) == count
    serial_floor = delay_s * count
    # Generous bound: even a modest pool must beat half the serial time. The
    # point is overlap existing at all, not a precise speedup on a busy CI box.
    assert elapsed < serial_floor / 2, (
        f"{count} x {delay_s}s reads took {elapsed:.2f}s, which is not "
        f"meaningfully better than the {serial_floor:.2f}s serial floor"
    )


def test_window_never_exceeds_depth(tmp_path: Path) -> None:
    """if the in-flight window exceeds depth then memory is unbounded"""
    depth = 3
    live = 0
    peak = 0
    lock = threading.Lock()
    gate = threading.Event()

    class CountingPath(type(tmp_path)):  # type: ignore[misc]
        def read_bytes(self) -> bytes:
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
            gate.wait(0.05)
            with lock:
                live -= 1
            return super().read_bytes()

    paths = [CountingPath(str(_write(tmp_path, f"w{i}.bin", 8))) for i in range(20)]
    consumed = list(read_ahead(paths, depth=depth, workers=8))

    assert len(consumed) == 20
    assert peak <= depth, f"had {peak} reads in flight against depth {depth}"


def test_byte_budget_caps_the_window(tmp_path: Path) -> None:
    """if the byte budget is not enforced then large files OOM the Mac"""
    size = 1000
    live = 0
    peak = 0
    lock = threading.Lock()

    class CountingPath(type(tmp_path)):  # type: ignore[misc]
        def read_bytes(self) -> bytes:
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
            time.sleep(0.03)
            with lock:
                live -= 1
            return super().read_bytes()

    paths = [CountingPath(str(_write(tmp_path, f"b{i}.bin", size))) for i in range(12)]
    # Depth would allow 12, the budget allows 2.
    consumed = list(read_ahead(paths, depth=12, workers=8, max_bytes=2 * size))

    assert len(consumed) == 12
    assert peak <= 2, f"byte budget allowed {peak} concurrent reads, expected <= 2"


def test_oversized_file_still_progresses(tmp_path: Path) -> None:
    """if a file larger than the budget stalls then the feeder deadlocks"""
    paths = [
        _write(tmp_path, "small.bin", 10),
        _write(tmp_path, "huge.bin", 5000),
        _write(tmp_path, "after.bin", 10),
    ]
    got = list(read_ahead(paths, depth=4, workers=2, max_bytes=100))
    assert [r.path.name for r in got] == ["small.bin", "huge.bin", "after.bin"]


def test_stat_is_taken_after_the_read(tmp_path: Path) -> None:
    """if the stat precedes the read then iCloud materialisation poisons signatures"""
    order: list[str] = []
    path = _write(tmp_path, "order.bin", 24)

    class OrderedPath(type(tmp_path)):  # type: ignore[misc]
        def read_bytes(self) -> bytes:
            order.append("read")
            return super().read_bytes()

        def stat(self, *args: object, **kwargs: object):  # type: ignore[override]
            order.append("stat")
            return super().stat(*args, **kwargs)

    list(read_ahead([OrderedPath(str(path))], depth=1, workers=1))
    # The sizing stat comes first by design; what must never happen is the
    # signature stat landing before the read that materialises the file.
    assert order[-1] == "stat"
    assert "read" in order
    assert order.index("read") < len(order) - 1


def test_read_error_propagates(tmp_path: Path) -> None:
    """if a read error is swallowed then a corrupt path silently drops a track"""
    good = _write(tmp_path, "good.bin", 16)
    missing = tmp_path / "gone.bin"
    with pytest.raises(OSError):
        list(read_ahead([good, missing], depth=2, workers=2))


def test_abandoning_the_generator_terminates(tmp_path: Path) -> None:
    """if abandoning the generator hangs then a mid-run abort never terminates"""
    paths = [_write(tmp_path, f"a{i}.bin", 16) for i in range(40)]
    stream = read_ahead(paths, depth=8, workers=4)
    assert isinstance(next(stream), ReadyFile)
    started = time.perf_counter()
    stream.close()
    assert time.perf_counter() - started < 5.0, "generator close did not return"


@pytest.mark.parametrize(
    "kwargs", [{"depth": 0}, {"workers": 0}, {"max_bytes": 0}]
)
def test_rejects_nonsense_bounds(tmp_path: Path, kwargs: dict[str, int]) -> None:
    """if invalid bounds are accepted then the window is silently unbounded"""
    paths = [_write(tmp_path, "x.bin", 8)]
    with pytest.raises(ValueError):
        list(read_ahead(paths, **kwargs))  # type: ignore[arg-type]
