"""Act at real moments inside a stem read, without replacing any production code.

A stem part that is a FIFO makes each reader's ``open`` wait until a writer
opens the other end. The production metadata reader and decoder read it as an
ordinary WAV, fed here, and at each open this helper can run a real writer
first, so a test reaches the moment a decode is inside the bundle without
replacing any production function (AGENTS.md "No mocks and locked real
fixtures"). Whether the bundle lock is free at that moment is read from the
lock file itself, so a decode that still holds the lock gets no writer.

:func:`after_the_lock_is_released` is for a reader whose bundle loader
refuses a FIFO part (``load_stem_bundle``): it runs a real writer the moment
the reader first lets go of the bundle lock it took, seen through the
``fcntl.flock`` audit event rather than by polling the lock.

POSIX only: Windows has no ``os.mkfifo`` and no ``fcntl``.

-Claude
"""

from __future__ import annotations

import contextlib
import functools
import os
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

HAS_FIFO: bool = hasattr(os, "mkfifo")
SKIP_REASON: str = "needs os.mkfifo and fcntl (POSIX) to hold a real decode mid-bundle; Windows has neither"


def make_fifo(part: Path) -> bytes:
    """Replace the file ``part`` with a FIFO; return the bytes it held, to feed back."""
    data = part.read_bytes()
    part.unlink()
    os.mkfifo(part)
    return data


def _lock_held(lock_path: Path) -> bool:
    import fcntl

    if not lock_path.exists():
        return False
    with lock_path.open("a+b") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False


@dataclass
class Served:
    """What :func:`serving` saw: opens under the lock, and writers run without it."""

    opens_under_lock: int = 0
    writers_run: int = 0
    errors: list[BaseException] = field(default_factory=list)


def _open_writer_end(part: Path, stop: threading.Event) -> int | None:
    """The FIFO's write end once a reader has opened it; None once ``stop`` is set."""
    while not stop.is_set():
        try:
            if stat.S_ISFIFO(os.stat(part).st_mode):
                return os.open(part, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            pass  # no reader yet (ENXIO), or the bundle is mid-swap
        time.sleep(0.002)
    return None


def _feed(fd: int, data: bytes) -> None:
    """Write ``data``, put the FIFO's times back, then close (the reader's EOF).

    Writing to a FIFO moves its mtime, which every stem identity reads; the
    times go back before the close, so the reader cannot see them moved.
    """
    before = os.fstat(fd)
    os.set_blocking(fd, True)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
    except BrokenPipeError:
        pass  # a header read closes its end early
    finally:
        os.utime(fd, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.close(fd)


@contextlib.contextmanager
def serving(part: Path, data: bytes, lock_path: Path, writers: list[Callable[[], None]]) -> Iterator[Served]:
    """Feed ``data`` to every reader of the FIFO at ``part`` until the block exits.

    At each open made while ``lock_path`` is free, the next of ``writers``
    runs before the reader gets its bytes; an open made under the lock is
    only counted.
    """
    served = Served()
    stop = threading.Event()

    def run() -> None:
        while (fd := _open_writer_end(part, stop)) is not None:
            try:
                if _lock_held(lock_path):
                    served.opens_under_lock += 1
                elif writers:
                    writers.pop(0)()
                    served.writers_run += 1
            except BaseException as exc:  # noqa: BLE001 - reported by the test
                served.errors.append(exc)
            finally:
                _feed(fd, data)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield served
    finally:
        stop.set()
        thread.join(timeout=30)


@contextlib.contextmanager
def after_the_lock_is_released(lock_path: Path, write: Callable[[], None]) -> Iterator[Served]:
    """Run ``write`` once, as soon as the first holder of the lock at ``lock_path`` lets go.

    The release is observed, not polled for: every ``fcntl.flock`` raises
    the ``fcntl.flock`` audit event in the caller's thread just before the
    call, so an unlock of a descriptor naming ``lock_path`` is seen however
    briefly the lock was held. Polling with a non-blocking ``flock`` missed
    a hold of a few microseconds (an eviction claim's fingerprint), and the
    writer then never ran (#6821's pytest shard 5, Sat 17 Oct 2026). ``write``
    takes the lock itself, blocking, so it lands just after that release; a
    reader that keeps the lock through its whole read therefore gets the
    writer only after it.
    """
    import fcntl

    served = Served()
    released = threading.Event()
    stop = threading.Event()

    def watch(fd: int, operation: int) -> None:
        if released.is_set() or not operation & fcntl.LOCK_UN:
            return
        try:
            if os.path.samestat(os.fstat(fd), os.stat(lock_path)):
                released.set()
        except OSError:
            return  # not a descriptor this lock names, or the lock file is not there yet

    def run() -> None:
        while not stop.is_set():
            if not released.wait(0.01):
                continue
            try:
                write()
                served.writers_run += 1
            except BaseException as exc:  # noqa: BLE001 - reported by the test
                served.errors.append(exc)
            return

    _install_flock_hook()
    _flock_watchers.append(watch)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield served
    finally:
        _flock_watchers.remove(watch)
        stop.set()
        thread.join(timeout=30)


#: The callbacks the ``fcntl.flock`` audit hook hands each (fd, operation)
#: to; :func:`after_the_lock_is_released` adds one for its own block.
_flock_watchers: list[Callable[[int, int], None]] = []


def _on_audit(event: str, args: tuple[object, ...]) -> None:
    if event != "fcntl.flock" or not _flock_watchers:
        return
    fd, operation = args
    if isinstance(fd, int) and isinstance(operation, int):
        for watch in tuple(_flock_watchers):  # a block may end on another thread meanwhile
            watch(fd, operation)


@functools.cache
def _install_flock_hook() -> None:
    """Add the process-wide audit hook once; a hook cannot be removed, and
    with no watcher it returns at once."""
    sys.addaudithook(_on_audit)
