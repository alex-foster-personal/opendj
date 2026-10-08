"""The pipeline CLIs a refresh job has running, and stopping them on shutdown.

``ingest_job._run_cli`` starts each step (``apps.analysis.run`` and friends)
as a plain child of the engine, and the analysis CLI starts its own pool of
spawned workers under it. Nothing stopped them when the engine stopped. The
engine's lifespan shutdown stops the watchers that START jobs, but a step
already running kept going, so an engine that exited on a plain SIGTERM (the
shell's parent watch, or a ``kill <pid>``) left the CLI and its pool workers
behind: three analysis workers on demon-llama, Fri 2 Oct 2026, which then
blocked the DMG installer.

So every CLI is registered here while it runs, and the lifespan calls
:func:`stop_all` on the way out. A stopped step exits on a signal, which is
not one of the per-target exit codes a refresh job continues past, so the job
ends there rather than starting its next step. The whole tree is stopped, not
just the CLI: SIGTERM kills the CLI's own process outright, and its pool
workers would then wait on a parent that no longer exists.

Once :func:`stop_all` has run, the registry is closed: a step that starts
after it (the next step of a refresh job, or one that was mid-``Popen``) is
stopped the moment it registers, rather than outliving the engine. The
lifespan reopens it with :func:`reopen` on startup, so the next app started in
the same process (every lifespan test) does not inherit the refusal.

The stop also signals the CLI's process group, not only the children visible
at one snapshot. A pool worker forked after that snapshot is still in the
group and inherits the CLI's stdout pipe; leaving it alive blocks the step
thread in the pipe read, so the step outlives a shutdown that already killed
the CLI itself. ``_run_cli`` starts the CLI as a session leader so the group
is its tree and not the engine's. The group id is captured with the leader's
pid and a high-resolution start fingerprint. SIGTERM uses that id at once,
while the leader is still the process we recorded. SIGKILL uses it only after
the same identity check ``apps.engine_core.jobs.reap`` applies to a stored
pgid: a leader pid that is alive under a different start fingerprint, a group
whose every live member predates the recorded start, or an empty group is not
signalled. On Linux a group whose leader is already gone is signalled only
while the pidfd captured with that leader is still open. The fd pins the pid,
so the numeric id cannot have been taken by another session. Without the fd,
member create_time is not proof and the group is refused.
"""
from __future__ import annotations

import contextlib
import errno
import logging
import math
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

import psutil

from apps.shared.process_groups import (
    CREATE_TIME_TOLERANCE_S,
    live_group_members,
    signal_group,
)

log = logging.getLogger(__name__)

#: How long stopped processes get, all together, between terminate and kill.
STOP_GRACE_S: float = 3.0
#: How long :func:`stop_all` waits for killed processes to be gone. SIGKILL
#: cannot be caught, so this only covers the kernel tearing them down.
KILL_WAIT_S: float = 1.0
#: The longest :func:`stop_all` can take, however many CLIs are running and
#: however they treat SIGTERM. The shell's grace is budgeted against this.
STOP_ALL_MAX_S: float = STOP_GRACE_S + KILL_WAIT_S

_lock = threading.Lock()
_procs: set[subprocess.Popen[str]] = set()
#: Set by :func:`stop_all`, cleared by :func:`reopen`; changed under ``_lock``
#: so a register cannot slip between the snapshot and the close.
_closed = threading.Event()


def reopen() -> None:
    """Accept CLIs again; the lifespan calls this on startup."""
    with _lock:
        _closed.clear()


def register(proc: subprocess.Popen[str]) -> bool:
    """Track a running CLI until :func:`unregister` or :func:`stop_all`.

    After :func:`stop_all` the CLI is killed at once instead, with its
    descendants, and this returns False. It gets no SIGTERM grace: this runs
    in the job's own thread after :func:`stop_all` has returned, so nothing
    in the shutdown waits on it, and the interpreter can exit mid-grace
    before the SIGKILL rung, leaving a CLI that ignores SIGTERM behind.
    """
    with _lock:
        if not _closed.is_set():
            _procs.add(proc)
            return True
    log.warning("pipeline CLI pid %d started after shutdown began; killing it", proc.pid)
    _kill([proc])
    return False


def unregister(proc: subprocess.Popen[str]) -> None:
    with _lock:
        _procs.discard(proc)


def stop_all() -> int:
    """Stop every running CLI and its descendants within :data:`STOP_ALL_MAX_S`.

    Every process is signalled first and then all of them share ONE grace
    window, so the total is bounded no matter how many CLIs run or how many
    of them ignore SIGTERM. Waiting tree by tree, with a grace per wait,
    added up past the shell's own grace, and its SIGKILL then landed
    mid-reap with the pool workers still running.

    Closes the registry, see :func:`register`. Returns how many CLIs were
    running, for the log line.
    """
    with _lock:
        _closed.set()
        running = list(_procs)
        _procs.clear()
    if not running:
        return 0
    _stop(running)
    log.info("shutdown stopped %d running pipeline CLI(s)", len(running))
    return len(running)


def posix_stop_signal(kill: bool, platform: str | None = None) -> int | None:
    """The POSIX stop signal, or None on Windows where SIGKILL is missing.

    Windows never evaluates ``signal.SIGKILL`` or ``signal.SIGTERM``: those
    attributes are not present there, and stop still uses ``proc.kill()``
    and ``member.kill()``.
    """
    if (os.name if platform is None else platform) == "nt":
        return None
    return signal.SIGKILL if kill else signal.SIGTERM


class LeaderStartUnreadable(Exception):
    """The pid is alive, but its high-resolution start fingerprint cannot be read.

    Callers refuse the process group. There is no fallback to a one-second
    clock, ``ps -o lstart``, or an integer ``create_time``.
    """


@dataclass(frozen=True, slots=True)
class LeaderStart:
    """Exact start identity of one process incarnation.

    ``ticks`` is the platform start clock, never truncated to whole seconds.
    Linux: ``/proc/<pid>/stat`` field 22, clock ticks since boot (``USER_HZ``,
    10 ms when ``CLK_TCK`` is 100). macOS: the ``kinfo_proc`` start timeval in
    microseconds since the epoch, via psutil's float of that timeval.

    ``tiebreak`` separates leaders that share one tick. On Linux the tick is
    10 ms, and psutil's ``create_time()`` is that tick divided by ``CLK_TCK``
    plus the integer boot second, so two leaders started in the same tick
    compare equal. Measured on nucbox-wsl (kernel 6.18, ``CLK_TCK`` 100,
    psutil 7.2.2): 24 of 25 leaders spawned after the previous one had been
    reaped still shared a tick. The tiebreak is the pidfd inode. On a host
    where that inode is reused as soon as the leader exits, it does not
    separate the next leader. ``proc_ino`` is the ``/proc/<pid>`` inode, which
    is allocated for that incarnation and is not the pidfd inode. On macOS
    the timeval is already microseconds, and both ``tiebreak`` and
    ``proc_ino`` are 0.
    """

    ticks: int
    tiebreak: int
    proc_ino: int


@dataclass(frozen=True)
class _CapturedGroup:
    """A CLI process group plus the leader identity taken while it still led.

    ``pgid`` and ``leader_pid`` are the same number at capture (the CLI is the
    session leader). Both are kept because that number can be reused by
    another session after the leader is reaped. ``fingerprint`` tells the two
    incarnations apart. ``started_at`` is the psutil ``create_time`` used only
    as the floor for live members (``CREATE_TIME_TOLERANCE_S``); it is not
    the leader identity.
    """

    pgid: int
    leader_pid: int
    started_at: float
    fingerprint: LeaderStart
    # Linux pidfd opened at capture. None on other platforms, or when the
    # leader exited before the open. Closed by ``_stop`` / ``_kill``.
    leader_pidfd: int | None = None


def _owned_groups(running: list[subprocess.Popen[str]]) -> list[_CapturedGroup]:
    """Each CLI's process group and leader identity, while the leader is alive."""
    groups: list[_CapturedGroup] = []
    for proc in running:
        group = _capture_group(proc)
        if group is not None:
            groups.append(group)
    return groups


def _capture_group(proc: subprocess.Popen[str]) -> _CapturedGroup | None:
    """Record the leader pid and its high-resolution start fingerprint.

    The member floor still uses ``Process.create_time()``. The identity
    compared before SIGKILL is :func:`read_leader_start`, which does not
    fall back to a one-second clock. A leader whose fingerprint cannot be
    read is left out of the group signal.
    """
    pgid = _own_group(proc)
    if pgid is None:
        return None
    try:
        started_at = psutil.Process(proc.pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    try:
        fingerprint = read_leader_start(proc.pid)
    except LeaderStartUnreadable:
        log.warning(
            "pipeline CLI pid %d start fingerprint cannot be read; "
            "its process group will not be signalled",
            proc.pid,
        )
        return None
    if fingerprint is None:
        return None
    return _CapturedGroup(
        pgid=pgid,
        leader_pid=proc.pid,
        started_at=started_at,
        fingerprint=fingerprint,
        leader_pidfd=_open_leader_pidfd(proc.pid),
    )


def _stop(running: list[subprocess.Popen[str]]) -> None:
    descendants = [member for proc in running for member in _descendants(proc)]
    groups = _owned_groups(running)
    try:
        _signal(running, descendants, groups, kill=False)
        alive = _wait(running, descendants, STOP_GRACE_S)
        _signal(running, alive, groups, kill=True)
        _wait(running, alive, KILL_WAIT_S)
    finally:
        _close_leader_pidfds(groups)


def _kill(running: list[subprocess.Popen[str]]) -> None:
    descendants = [member for proc in running for member in _descendants(proc)]
    groups = _owned_groups(running)
    try:
        _signal(running, descendants, groups, kill=True)
        _wait(running, descendants, KILL_WAIT_S)
    finally:
        _close_leader_pidfds(groups)


def _own_group(proc: subprocess.Popen[str]) -> int | None:
    """The CLI's process group, only when this pid leads it.

    ``killpg`` of a pid that is not a group leader is either ``ESRCH`` or, if
    the id were ever 0, the caller's own group. A CLI that did not
    ``setsid`` shares the engine's group and must not be signalled that way.
    The leader check is what makes the group signal the CLI's tree alone.
    """
    if os.name == "nt":
        return None
    pid = proc.pid
    if pid <= 0:
        return None
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        return None
    if pgid != pid:
        return None
    return pgid


def read_leader_start(pid: int) -> LeaderStart | None:
    """The live leader's start fingerprint, or None when that pid is not live.

    A zombie is gone for this purpose: its pid has not been reused, and it is
    not proof that the group still belongs to the CLI. A live pid whose
    high-resolution clock cannot be read raises :class:`LeaderStartUnreadable`
    rather than matching on a coarser clock.

    Linux reads ``/proc/<pid>/stat`` field 22, the pidfd inode, and the
    ``/proc/<pid>`` inode. macOS reads psutil's float ``create_time``, which
    is the ``kinfo_proc`` start timeval in microseconds. Any other platform
    raises.
    """
    if pid <= 0:
        raise LeaderStartUnreadable(f"pid {pid} is not a process")
    if sys.platform == "linux":
        return _linux_leader_start(pid)
    if sys.platform == "darwin":
        return _darwin_leader_start(pid)
    raise LeaderStartUnreadable(
        f"no high-resolution process start time on {sys.platform}"
    )


def _linux_stat_start(pid: int) -> tuple[str, int]:
    """Process state and ``/proc/<pid>/stat`` field 22 (starttime ticks)."""
    path = f"/proc/{pid}/stat"
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except FileNotFoundError as exc:
        raise ProcessLookupError(pid) from exc
    except PermissionError as exc:
        raise LeaderStartUnreadable(f"{path}: {exc}") from exc
    rpar = data.rfind(b")")
    if rpar < 0:
        raise LeaderStartUnreadable(f"{path} has no comm field")
    fields = data[rpar + 2 :].split()
    # Field 3 (state) is fields[0]. Field 22 (starttime) is fields[19].
    if len(fields) < 20:
        raise LeaderStartUnreadable(
            f"{path} has {len(fields)} fields after comm; field 22 is missing"
        )
    state = fields[0].decode("ascii", "replace")
    try:
        ticks = int(fields[19])
    except ValueError as exc:
        raise LeaderStartUnreadable(f"{path} starttime is not an int") from exc
    return state, ticks


def _linux_proc_ino(pid: int) -> int:
    """Inode of ``/proc/<pid>``. Distinct from the pidfd inode.

    A pidfd inode can be recycled as soon as the leader is reaped, inside the
    same 10 ms start tick as the next leader. The proc inode is a different
    number for that new incarnation.
    """
    path = f"/proc/{pid}"
    try:
        inode = os.stat(path).st_ino
    except FileNotFoundError as exc:
        raise ProcessLookupError(pid) from exc
    except PermissionError as exc:
        raise LeaderStartUnreadable(f"{path}: {exc}") from exc
    if inode <= 0:
        raise LeaderStartUnreadable(f"{path} inode is {inode}")
    return inode


def _linux_incarnation(pid: int) -> int:
    """Pidfd inode of this pid. The same number on every open of this pid.

    On pidfs (Linux 6.9 and later) the inode belongs to the struct pid, so
    a second ``pidfd_open`` of the same live pid returns the same inode.
    Older kernels use the singleton anonymous inode, which also matches
    across reads. The fd opened here is closed. The fd kept for the kill
    rung is :func:`_open_leader_pidfd`.
    """
    try:
        fd = os.pidfd_open(pid)
    except AttributeError as exc:
        raise LeaderStartUnreadable("os.pidfd_open is not available") from exc
    except ProcessLookupError:
        raise
    except PermissionError as exc:
        raise LeaderStartUnreadable(f"pidfd_open({pid}): {exc}") from exc
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            raise ProcessLookupError(pid) from exc
        raise LeaderStartUnreadable(f"pidfd_open({pid}): {exc}") from exc
    try:
        return os.fstat(fd).st_ino
    finally:
        os.close(fd)


def _linux_leader_start(pid: int) -> LeaderStart | None:
    try:
        state, ticks = _linux_stat_start(pid)
        proc_ino = _linux_proc_ino(pid)
    except ProcessLookupError:
        return None
    if state == "Z":
        return None
    try:
        incarnation = _linux_incarnation(pid)
        _state_again, ticks_again = _linux_stat_start(pid)
        proc_ino_again = _linux_proc_ino(pid)
    except ProcessLookupError:
        return None
    if _state_again == "Z":
        return None
    if ticks_again != ticks or proc_ino_again != proc_ino:
        raise LeaderStartUnreadable(
            f"pid {pid} start identity changed from ticks={ticks} "
            f"proc_ino={proc_ino} to ticks={ticks_again} "
            f"proc_ino={proc_ino_again} while it was being read; "
            "refused to guess which incarnation it is"
        )
    return LeaderStart(ticks=ticks, tiebreak=incarnation, proc_ino=proc_ino)


def _darwin_leader_start(pid: int) -> LeaderStart | None:
    """macOS start timeval in microseconds. No one-second fallback."""
    try:
        leader = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return None
    try:
        if leader.status() == psutil.STATUS_ZOMBIE:
            return None
        created = leader.create_time()
    except psutil.NoSuchProcess:
        return None
    except psutil.AccessDenied as exc:
        raise LeaderStartUnreadable(
            f"pid {pid} kinfo_proc start time cannot be read: {exc}"
        ) from exc
    if isinstance(created, bool) or not isinstance(created, (int, float)):
        raise LeaderStartUnreadable(
            f"pid {pid} create_time is {created!r}, not a timeval"
        )
    if not math.isfinite(created) or created < 0:
        raise LeaderStartUnreadable(
            f"pid {pid} create_time {created!r} is not a start timeval"
        )
    # psutil's macOS create_time is tv_sec + tv_usec/1e6. Keep the microseconds.
    whole = int(created)
    micros = whole * 1_000_000 + round((created - whole) * 1_000_000)
    return LeaderStart(ticks=micros, tiebreak=0, proc_ino=0)


def _open_leader_pidfd(pid: int) -> int | None:
    """Pin ``pid`` until :func:`_close_leader_pidfds`, or None when it cannot.

    Linux does not reuse a pid while a pidfd to it is open. The kill rung
    uses that pin when the leader has already exited. Other platforms have
    no pidfd. ``ESRCH`` means the leader exited before the open.
    """
    if sys.platform != "linux":
        return None
    try:
        return os.pidfd_open(pid)
    except ProcessLookupError:
        return None
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return None
        log.warning("pidfd_open(%d) did not pin the leader: %s", pid, exc)
        return None


def _close_leader_pidfds(groups: list[_CapturedGroup]) -> None:
    for group in groups:
        if group.leader_pidfd is None:
            continue
        with contextlib.suppress(OSError):
            os.close(group.leader_pidfd)


def _dead_leader_pin_refusal(group: _CapturedGroup) -> str | None:
    """None when a not-live leader's group is still the recorded incarnation.

    On Linux the captured pidfd has to be open, and a re-read of the leader
    pid has to be absent or the recorded fingerprint. Member create_time is
    not enough: a numeric pgid reused after the leader read would also look
    newer than the floor. Other platforms have no pidfd, so the member floor
    stays the check.
    """
    if sys.platform != "linux":
        return None
    fd = group.leader_pidfd
    if fd is None:
        return (
            f"process group {group.pgid} leader {group.leader_pid} is not live "
            "and no pidfd pins that incarnation; refused to signal from "
            "member create_time alone"
        )
    try:
        os.fstat(fd)
    except OSError as exc:
        return (
            f"pidfd for leader {group.leader_pid} is not open ({exc}); "
            f"refused to signal process group {group.pgid}"
        )
    try:
        live = read_leader_start(group.leader_pid)
    except LeaderStartUnreadable as exc:
        return (
            f"pid {group.leader_pid} is not a readable leader ({exc}); "
            f"refused to signal process group {group.pgid}"
        )
    if live is not None and live != group.fingerprint:
        recorded = group.fingerprint
        return (
            f"pid {group.leader_pid} is alive with create_time "
            f"ticks={live.ticks} tiebreak={live.tiebreak} "
            f"proc_ino={live.proc_ino} which does not "
            f"match the recorded leader start ticks={recorded.ticks} "
            f"tiebreak={recorded.tiebreak} proc_ino={recorded.proc_ino}"
        )
    return None


def _kill_group_refusal(group: _CapturedGroup) -> str | None:
    """Why SIGKILL must not be sent, or None when the group is still this CLI.

    Same three refusals as a stored pgid in ``reap``: the leader pid is alive
    under a different start fingerprint, every live member predates the
    recorded leader start, or the group is empty. A worker forked into the
    original group after the child snapshot is newer than that start and is
    still killed.
    """
    try:
        live = read_leader_start(group.leader_pid)
    except LeaderStartUnreadable as exc:
        return (
            f"pid {group.leader_pid} is alive but its create_time cannot be read "
            f"({exc}); refused to signal process group {group.pgid}"
        )
    if live is not None:
        recorded = group.fingerprint
        if live != recorded:
            return (
                f"pid {group.leader_pid} is alive with create_time "
                f"ticks={live.ticks} tiebreak={live.tiebreak} "
                f"proc_ino={live.proc_ino} which does not "
                f"match the recorded leader start ticks={recorded.ticks} "
                f"tiebreak={recorded.tiebreak} proc_ino={recorded.proc_ino}"
            )
        return None
    return _members_refusal(group)


def _members_refusal(group: _CapturedGroup) -> str | None:
    """Refuse an empty group, or one whose live members all predate the leader."""
    floor = group.started_at - CREATE_TIME_TOLERANCE_S
    predating: list[int] = []
    unread: list[int] = []
    for proc in live_group_members(group.pgid):
        try:
            created = proc.create_time()
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            unread.append(proc.pid)
            continue
        if created >= floor:
            refusal = _dead_leader_pin_refusal(group)
            if refusal is not None:
                return refusal
            return None
        predating.append(proc.pid)
    if not predating and not unread:
        return f"process group {group.pgid} is empty"
    if predating and not unread:
        return (
            f"every live member of process group {group.pgid} ({predating}) "
            "predates the recorded leader start"
        )
    return (
        f"process group {group.pgid} has no live member created at or after "
        f"the recorded leader start (unreadable {unread}, predating {predating})"
    )


def _signal_kill_if_ours(group: _CapturedGroup, sig: int) -> str | None:
    """Send ``sig`` only when ``group`` is still this CLI.

    Returns the refusal when ``signal_group`` was not called, or None when it
    was. Callers on the kill rung must not signal a refused pgid.
    """
    reason = _kill_group_refusal(group)
    if reason is None:
        signal_group(group.pgid, sig)
        return None
    if reason.endswith("is empty"):
        log.info("shutdown did not signal process group %d: %s", group.pgid, reason)
    else:
        log.warning("shutdown did not signal process group %d: %s", group.pgid, reason)
    return reason


def _signal(
    running: list[subprocess.Popen[str]],
    members: list[psutil.Process],
    groups: list[_CapturedGroup],
    *,
    kill: bool,
) -> None:
    """Terminate (or kill) every CLI still running and every listed descendant.

    The captured groups go first: a worker forked after the snapshot is in
    that group and is not in ``members``. The ids are taken before the first
    signal so the kill rung still names that group after the leader has been
    reaped. SIGKILL is sent only when :func:`_signal_kill_if_ours` still
    recognizes the group. SIGTERM is not rechecked: the leader was alive at
    capture, and the late worker is forked inside that signal's handler.
    """
    sig = posix_stop_signal(kill)
    if sig is not None:
        for group in groups:
            if kill:
                _signal_kill_if_ours(group, sig)
            else:
                signal_group(group.pgid, sig)
    for proc in running:
        if proc.poll() is not None:
            continue
        with contextlib.suppress(ProcessLookupError):
            if kill:
                proc.kill()
            else:
                proc.terminate()
    for member in members:
        with contextlib.suppress(psutil.NoSuchProcess):
            if kill:
                member.kill()
            else:
                member.terminate()


def _wait(
    running: list[subprocess.Popen[str]], members: list[psutil.Process], within_s: float
) -> list[psutil.Process]:
    """Wait up to ``within_s`` IN TOTAL for all of them; return the members still alive."""
    deadline = time.monotonic() + within_s
    for proc in running:
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
    if not members:
        return []
    _, alive = psutil.wait_procs(members, timeout=max(0.0, deadline - time.monotonic()))
    return list(alive)


def _descendants(proc: subprocess.Popen[str]) -> list[psutil.Process]:
    """Everything under ``proc``, listed BEFORE anything is signalled.

    Once the CLI dies its workers are reparented and no longer reachable as
    its children.

    The CLI itself is signalled and waited through its ``Popen``, never
    through psutil. psutil's wait would reap it behind the job thread's back,
    and ``Popen.wait`` reports a child it can no longer reap as exit 0, so a
    stopped step would read as a successful one.
    """
    try:
        return psutil.Process(proc.pid).children(recursive=True)
    except psutil.NoSuchProcess:
        return []


__all__ = ["KILL_WAIT_S", "STOP_ALL_MAX_S", "STOP_GRACE_S", "register", "stop_all", "unregister"]
