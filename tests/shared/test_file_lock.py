"""The exclusive file-lock seam, proven with real files and real second processes.

[if] one process holds the lock [then] another process is refused at once, [else stop].

Each arm runs on its own platform only: ``fcntl.flock`` exists only on POSIX
and ``msvcrt.locking`` only on Windows, so the other arm's tests skip naming
that API. A child interpreter is the contender because both primitives are
per open file description, and the property callers rely on (the engine
singleton, the CloudSync round) is exclusion BETWEEN processes.
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
from pathlib import Path

import pytest

from apps.shared import file_lock
from tests.waits import EXTERNAL_STATE_GUARD_S

REPO_ROOT = Path(__file__).resolve().parents[2]
POSIX_ARM = pytest.mark.skipif(sys.platform == "win32", reason="fcntl.flock arm: fcntl exists only on POSIX")
NT_ARM = pytest.mark.skipif(sys.platform != "win32", reason="msvcrt.locking arm: msvcrt exists only on Windows")
ARMS = [pytest.param("posix", marks=POSIX_ARM), pytest.param("nt", marks=NT_ARM)]

EXIT_ACQUIRED, EXIT_REFUSED = 0, 3

pytestmark = pytest.mark.requirement("INSTALL-38")

#: The contender: one non-blocking try, exit code says which way it went.
CONTENDER = (
    "import os, sys\n"
    "from apps.shared import file_lock\n"
    "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
    f"sys.exit({EXIT_ACQUIRED} if file_lock.try_lock(fd) else {EXIT_REFUSED})\n"
)

#: A holder that takes the lock, says so, then blocks until killed.
HOLDER = (
    "import os, sys\n"
    "from apps.shared import file_lock\n"
    "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
    "assert file_lock.try_lock(fd)\n"
    "print('held', flush=True)\n"
    "sys.stdin.read()\n"
)


def _contend(path: Path) -> int:
    done = subprocess.run(
        [sys.executable, "-c", CONTENDER, str(path)],
        cwd=REPO_ROOT,
        timeout=EXTERNAL_STATE_GUARD_S,
        check=False,
    )
    return done.returncode


def _open(path: Path) -> int:
    return os.open(path, os.O_RDWR | os.O_CREAT, 0o600)


@pytest.mark.parametrize("arm", ARMS)
def test_a_second_process_is_refused_at_once_while_held(tmp_path: Path, arm: str) -> None:
    """
    [if] this process holds the lock [then] a child's try refuses without blocking, [else stop].
    """
    lock_path = tmp_path / "x.lock"
    fd = _open(lock_path)
    try:
        assert file_lock.try_lock(fd)
        assert _contend(lock_path) == EXIT_REFUSED
        file_lock.unlock(fd)
        assert _contend(lock_path) == EXIT_ACQUIRED
    finally:
        os.close(fd)


@pytest.mark.parametrize("arm", ARMS)
def test_the_os_drops_the_lock_when_its_holder_process_dies(tmp_path: Path, arm: str) -> None:
    """
    [if] the holding process is killed without unlocking [then] the lock is free again, [else stop].
    """
    lock_path = tmp_path / "x.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(lock_path)],
        cwd=REPO_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        fd = _open(lock_path)
        try:
            assert not file_lock.try_lock(fd), "a live holder's lock was taken"
        finally:
            os.close(fd)
    finally:
        holder.kill()
        holder.wait(timeout=EXTERNAL_STATE_GUARD_S)
        assert holder.stdin is not None and holder.stdout is not None
        holder.stdin.close()
        holder.stdout.close()
    assert _contend(lock_path) == EXIT_ACQUIRED


@pytest.mark.parametrize("arm", ARMS)
def test_held_content_stays_readable_and_the_position_is_kept(tmp_path: Path, arm: str) -> None:
    """
    [if] the lock is held [then] others read the file and the fd stays put, [else stop].
    """
    lock_path = tmp_path / "x.lock"
    fd = _open(lock_path)
    try:
        os.write(fd, b'{"pid": 1}')
        assert file_lock.try_lock(fd)
        assert os.lseek(fd, 0, os.SEEK_CUR) == len(b'{"pid": 1}')
        assert lock_path.read_bytes() == b'{"pid": 1}'
        file_lock.unlock(fd)
        assert os.lseek(fd, 0, os.SEEK_CUR) == len(b'{"pid": 1}')
    finally:
        os.close(fd)


# ----- Windows lock byte per protocol (PR #6399 review) -----
# These proofs call the real ``msvcrt.locking`` and the real seam. There is
# no stand-in module and no ``sys.platform`` override: every other platform
# skips, naming the API it does not have. The ``sys.platform`` guard is also
# what keeps mypy (platform darwin) from scoring ``msvcrt`` on other hosts.

# Win32 codes a held byte can surface as, beside the CRT's EACCES.
_SHARING_VIOLATION = 32
_LOCK_VIOLATION = 33

_REAL_MSVCRT = pytest.mark.skipif(
    sys.platform != "win32",
    reason="UNAVAILABLE: msvcrt.locking exists only on Windows",
)


def _held_byte(exc: OSError) -> bool:
    """True when ``msvcrt.locking`` refused because that byte is already held."""
    winerror = getattr(exc, "winerror", None)
    return exc.errno == errno.EACCES or winerror in (_SHARING_VIOLATION, _LOCK_VIOLATION)


@_REAL_MSVCRT
def test_sync_lock_keeps_byte_zero_so_a_pre_upgrade_round_still_excludes_it(tmp_path: Path) -> None:
    """
    [if] one handle holds byte 0 with msvcrt.locking [then] the sync offset refuses, [else stop].
    """
    if sys.platform != "win32":
        return
    import msvcrt

    lock_path = tmp_path / ".sync.lock"
    old, new = _open(lock_path), _open(lock_path)
    try:
        os.lseek(old, file_lock.SYNC_NT_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(old, msvcrt.LK_NBLCK, 1)
        assert file_lock.try_lock(new, nt_offset=file_lock.SYNC_NT_LOCK_OFFSET) is False
        os.lseek(old, file_lock.SYNC_NT_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(old, msvcrt.LK_UNLCK, 1)
        assert file_lock.try_lock(new, nt_offset=file_lock.SYNC_NT_LOCK_OFFSET) is True
        file_lock.unlock(new, nt_offset=file_lock.SYNC_NT_LOCK_OFFSET)
        os.lseek(old, file_lock.SYNC_NT_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(old, msvcrt.LK_NBLCK, 1)
        os.lseek(old, file_lock.SYNC_NT_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(old, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(old)
        os.close(new)


@_REAL_MSVCRT
def test_engine_lock_byte_stays_past_the_content_on_windows(tmp_path: Path) -> None:
    """
    [if] the default offset is used on Windows [then] it locks the distant byte only, [else stop].
    """
    if sys.platform != "win32":
        return
    import msvcrt

    content = b'{"pid": 1}'
    fd = _open(tmp_path / ".engine.lock")
    probe = _open(tmp_path / ".engine.lock")
    try:
        os.write(fd, content)
        assert file_lock.try_lock(fd) is True
        assert os.lseek(fd, 0, os.SEEK_CUR) == len(content)
        os.lseek(probe, 0, os.SEEK_SET)
        msvcrt.locking(probe, msvcrt.LK_NBLCK, 1)
        os.lseek(probe, 0, os.SEEK_SET)
        msvcrt.locking(probe, msvcrt.LK_UNLCK, 1)
        os.lseek(probe, file_lock.NT_LOCK_OFFSET, os.SEEK_SET)
        with pytest.raises(OSError) as caught:
            msvcrt.locking(probe, msvcrt.LK_NBLCK, 1)
        assert _held_byte(caught.value), caught.value
        assert os.lseek(fd, 0, os.SEEK_CUR) == len(content)
        file_lock.unlock(fd)
    finally:
        os.close(fd)
        os.close(probe)


@_REAL_MSVCRT
def test_sync_flock_for_refuses_while_a_real_byte_zero_lock_is_held(tmp_path: Path) -> None:
    """
    [if] one handle holds byte 0 with msvcrt.locking [then] sync_flock_for refuses, [else stop].
    """
    if sys.platform != "win32":
        return
    import msvcrt

    from apps.sync_hub import single_flight

    lock_path = tmp_path / single_flight.SYNC_LOCK_FILENAME
    holder = _open(lock_path)
    try:
        os.lseek(holder, file_lock.SYNC_NT_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(holder, msvcrt.LK_NBLCK, 1)
        with pytest.raises(single_flight.SyncInProgressError):
            with single_flight.sync_flock_for(tmp_path):
                pass
        os.lseek(holder, file_lock.SYNC_NT_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(holder, msvcrt.LK_UNLCK, 1)
        with single_flight.sync_flock_for(tmp_path):
            pass
    finally:
        os.close(holder)
