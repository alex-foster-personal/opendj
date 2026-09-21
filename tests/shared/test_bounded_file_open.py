"""Unit tests for subprocess-isolated bounded file open (#2749).

[if] a file is readable [then] the probe returns ok inside its timeout, [else stop].
[if] an open blocks (writer-less FIFO) [then] the probe times out and reaps its worker, [else stop].
[if] a probe just timed out [then] the next probe of a readable file is still fast, [else stop].
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import psutil
import pytest

from apps.shared.bounded_file_open import (
    _process_kwargs,
    _terminate_probe_worker,
    _worker_command,
    probe_readable_byte,
)

pytestmark = pytest.mark.requirement("PREFLIGHT-01")

PROBE_TIMEOUT_S = 0.5
PROBE_SLACK_S = 1.5


def _probe_workers_alive(path: Path) -> list[psutil.Process]:
    path_text = str(path)
    alive: list[psutil.Process] = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
            if any("bounded_file_open_worker" in part for part in cmdline) and any(
                path_text == part for part in cmdline
            ):
                alive.append(proc)
        except (psutil.Error, ProcessLookupError):
            continue
    return alive


def _spawn_blocking_worker(path: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        _worker_command(path),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **(_process_kwargs()),
    )


def _cleanup_worker(proc: subprocess.Popen[bytes]) -> None:
    try:
        _terminate_probe_worker(proc)
        proc.wait()
    finally:
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()


def test_reads_one_byte_from_real_file(tmp_path: Path) -> None:
    target = tmp_path / "readable.mp3"
    target.write_bytes(b"ID3")
    start = time.monotonic()
    result = probe_readable_byte(target, timeout_s=PROBE_TIMEOUT_S)
    elapsed = time.monotonic() - start
    assert result.outcome == "ok"
    assert elapsed < PROBE_TIMEOUT_S


def test_fifo_without_writer_times_out_and_leaves_no_child(tmp_path: Path) -> None:
    fifo = tmp_path / "blocked.fifo"
    os.mkfifo(fifo)
    start = time.monotonic()
    result = probe_readable_byte(fifo, timeout_s=PROBE_TIMEOUT_S)
    elapsed = time.monotonic() - start
    assert result.outcome == "timeout"
    assert elapsed < PROBE_TIMEOUT_S + PROBE_SLACK_S
    assert not _probe_workers_alive(fifo)


def test_probe_workers_alive_ignores_worker_on_different_fifo_path(tmp_path: Path) -> None:
    """[if] a worker blocks on another fifo [then] scoped probe for this fifo is empty, [else stop]."""
    fifo_here = tmp_path / "here.fifo"
    fifo_other = tmp_path / "other.fifo"
    os.mkfifo(fifo_here)
    os.mkfifo(fifo_other)

    other_worker = _spawn_blocking_worker(fifo_other)
    try:
        assert not _probe_workers_alive(fifo_here)
    finally:
        _cleanup_worker(other_worker)


def test_probe_workers_alive_detects_worker_for_same_fifo_path(tmp_path: Path) -> None:
    """[if] a worker blocks on this fifo [then] scoped probe reports it alive, [else stop]."""
    fifo = tmp_path / "blocked.fifo"
    os.mkfifo(fifo)

    worker = _spawn_blocking_worker(fifo)
    try:
        assert _probe_workers_alive(fifo)
    finally:
        _cleanup_worker(worker)


def test_second_probe_after_fifo_timeout_still_fast(tmp_path: Path) -> None:
    fifo = tmp_path / "blocked.fifo"
    os.mkfifo(fifo)
    first = probe_readable_byte(fifo, timeout_s=PROBE_TIMEOUT_S)
    assert first.outcome == "timeout"

    target = tmp_path / "readable.mp3"
    target.write_bytes(b"ID3")
    start = time.monotonic()
    second = probe_readable_byte(target, timeout_s=PROBE_TIMEOUT_S)
    elapsed = time.monotonic() - start
    assert second.outcome == "ok"
    assert elapsed < PROBE_TIMEOUT_S + 0.5
