"""Unit tests for subprocess-isolated bounded file open (#2749).

[if] a file is readable [then] the probe returns ok inside its timeout, [else stop].
[if] a read blocks (writer-less FIFO, silent named pipe) [then] the probe times out and reaps its worker, [else stop].
[if] a probe just timed out [then] the next probe of a readable file is still fast, [else stop].

The blocking path is a FIFO with no writer on POSIX and, on Windows (which has
no mkfifo), a named pipe whose server never writes: the worker's open succeeds
and its one-byte read blocks, the same shape as a stalled network mount.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

import psutil
import pytest

from apps.shared.bounded_file_open import (
    _parse_worker_stderr,
    _spawn_probe_worker,
    _stop_probe_worker,
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


@contextmanager
def _blocking_path(tmp_path: Path, name: str) -> Iterator[Path]:
    """A path whose one-byte read blocks until the context closes."""
    if sys.platform != "win32":
        fifo = tmp_path / f"{name}.fifo"
        os.mkfifo(fifo)
        yield fifo
        return
    from multiprocessing.connection import Listener

    sep = chr(92)  # a backslash: names live under two of them, ".", then "pipe"
    address = f"{sep}{sep}.{sep}pipe{sep}odj-bounded-open-{name}-{uuid.uuid4().hex}"
    listener = Listener(address=address, family="AF_PIPE")
    try:
        yield Path(address)
    finally:
        listener.close()


@contextmanager
def _blocking_worker(path: Path) -> Iterator[subprocess.Popen[bytes]]:
    proc, root = _spawn_probe_worker(path)
    try:
        time.sleep(0.5)  # long enough to be blocked inside its read
        yield proc
    finally:
        try:
            _stop_probe_worker(proc, root)
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
    with _blocking_path(tmp_path, "blocked") as fifo:
        start = time.monotonic()
        result = probe_readable_byte(fifo, timeout_s=PROBE_TIMEOUT_S)
        elapsed = time.monotonic() - start
        assert result.outcome == "timeout"
        assert elapsed < PROBE_TIMEOUT_S + PROBE_SLACK_S
        assert not _probe_workers_alive(fifo)


def test_probe_workers_alive_ignores_worker_on_different_fifo_path(tmp_path: Path) -> None:
    """[if] a worker blocks on another fifo [then] scoped probe for this fifo is empty, [else stop]."""
    with ExitStack() as stack:
        fifo_here = stack.enter_context(_blocking_path(tmp_path, "here"))
        fifo_other = stack.enter_context(_blocking_path(tmp_path, "other"))
        stack.enter_context(_blocking_worker(fifo_other))
        assert not _probe_workers_alive(fifo_here)


def test_probe_workers_alive_detects_worker_for_same_fifo_path(tmp_path: Path) -> None:
    """[if] a worker blocks on this fifo [then] scoped probe reports it alive, [else stop]."""
    with _blocking_path(tmp_path, "blocked") as fifo, _blocking_worker(fifo):
        assert _probe_workers_alive(fifo)


def test_second_probe_after_fifo_timeout_still_fast(tmp_path: Path) -> None:
    with _blocking_path(tmp_path, "blocked") as fifo:
        first = probe_readable_byte(fifo, timeout_s=PROBE_TIMEOUT_S)
    assert first.outcome == "timeout"

    target = tmp_path / "readable.mp3"
    target.write_bytes(b"ID3")
    start = time.monotonic()
    second = probe_readable_byte(target, timeout_s=PROBE_TIMEOUT_S)
    elapsed = time.monotonic() - start
    assert second.outcome == "ok"
    assert elapsed < PROBE_TIMEOUT_S + 0.5


def test_stopping_a_blocked_worker_leaves_no_process_behind(tmp_path: Path) -> None:
    """[if] a blocked probe worker is stopped [then] its whole tree is gone, launcher and interpreter alike, [else stop]."""
    with _blocking_path(tmp_path, "tree") as fifo:
        with _blocking_worker(fifo) as worker:
            tree = [worker.pid] + [c.pid for c in psutil.Process(worker.pid).children(recursive=True)]
        assert worker.returncode is not None
        assert [pid for pid in tree if psutil.pid_exists(pid)] == []


_REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_worker(command: list[str]) -> tuple[int, int | None]:
    env = dict(os.environ, PYTHONPATH=str(_REPO_ROOT))
    done = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=30, env=env, check=False)
    errno_value, _message = _parse_worker_stderr(done.stderr)
    return done.returncode, errno_value


@pytest.mark.parametrize("case", ["readable", "missing", "directory"])
def test_inline_probe_child_answers_exactly_like_the_module_worker(tmp_path: Path, case: str) -> None:
    """[if] the inline probe child and bounded_file_open_worker disagree on exit code or errno [then] the 503 mapping changed, [else stop].

    _worker_command runs an inline ``-I -S -c`` program (no ``apps`` import, so
    a cheaper child start) in place of ``-m apps.shared.bounded_file_open_worker``.
    The route maps the child's errno to 503 / 404, so the two must agree on
    every outcome. The ``readable`` row is the control: both must exit 0.
    """
    target = tmp_path / "subject.mp3"
    if case == "readable":
        target.write_bytes(b"ID3")
    elif case == "directory":
        target.mkdir()
    inline = _run_worker(_worker_command(target))
    module = _run_worker([sys.executable, "-m", "apps.shared.bounded_file_open_worker", str(target)])
    assert inline == module
    if case == "readable":
        assert inline == (0, None)
    else:
        assert inline[0] == 1 and inline[1] is not None


def test_probe_child_is_isolated_and_still_named_for_process_listings(tmp_path: Path) -> None:
    """[if] the probe child imports the apps package or loses the worker name in its cmdline [then] broken, [else stop].

    ``-I -S`` is the saving (bare interpreter start). The worker name in the
    cmdline is what ``_probe_workers_alive`` and operators grep for.
    """
    command = _worker_command(tmp_path / "x.mp3")
    assert command[0] == sys.executable
    assert command[1:4] == ["-I", "-S", "-c"]
    assert "bounded_file_open_worker" in command[4]
    assert "import apps" not in command[4] and "from apps" not in command[4]
