"""POSIX process-group liveness, signalling, and the wait that confirms death,
plus the cross-platform tree stop (``stop_tree``: process groups on POSIX,
``taskkill /T`` on Windows) that keeps both arms behind one seam.

Two subsystems terminate a worker tree they spawned with
``start_new_session=True``: ``apps/vocals/cli.py::_terminate_worker_tree`` and
``apps/engine_core/jobs/reap.py``. Both need the same answer to the same
deceptively simple question -- "is anything still running in process group N?"
-- and both got it wrong in the same way before this module existed.

THE TRAP THIS MODULE EXISTS TO CLOSE. ``killpg(pgid, 0)`` is the obvious
liveness probe and it is not one. A group whose every member is a ZOMBIE (dead,
but not yet waited on by its parent) keeps its pgid allocated, and on macOS it
answers that probe with EPERM rather than ESRCH. Read EPERM as "exists" and a
caller concludes the group survived, burns its entire grace budget polling a
corpse, and then reports a live worker that has in fact been dead the whole
time. Read it as "gone" and a group you genuinely may not signal looks reaped.
Neither reading is safe, because the syscall cannot distinguish the two cases.

So the probe is split in two. ``process_group_exists`` keeps the cheap syscall
and keeps its narrow meaning: the pgid is still ALLOCATED. ``group_has_live
_member`` answers the question callers actually have -- is anything in there
still EXECUTING -- by walking the process table and discounting zombies. The
walk costs an enumeration of every process on the box, which is why
``wait_group_gone`` runs the two on different cadences rather than picking one.

A member whose status cannot be read counts as LIVE throughout. Failing to
inspect something is not evidence that it is dead, and the asymmetry is
deliberate: over-reporting liveness costs a slower reap, under-reporting it
costs a leaked worker tree that every caller believes was collected.
"""

from __future__ import annotations

import contextlib
import ntpath
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping

import psutil

# psutil create_time and the wall clock we fall back to at spawn are both
# taken within milliseconds of the fork, so 2s is a generous identity window
# that still fails a pid recycled minutes later. One window, shared by the
# job reaper and CLI shutdown: the web UI must not import apps.engine_core
# to read it (.importlinter, webui-is-the-top-layer).
CREATE_TIME_TOLERANCE_S: float = 2.0

# Polling cadence for the cheap "is the pgid still allocated" syscall.
_POLL_S: float = 0.02

# The live-member walk enumerates every process on the box, so it runs on a
# far slower beat than the one-syscall group check it backs up.
_LIVE_CHECK_EVERY_S: float = 0.25


class WorkerCleanupError(RuntimeError):
    """The process group could not be confirmed dead.

    Shared by both callers on purpose: one failure mode deserves one exception
    type, so a caller that catches it does not have to know which subsystem
    spawned the tree. ``apps/vocals/cli.py`` re-exports it under its own name
    because its claim-retention path keys off exactly this error.
    """


def process_group_exists(pgid: int) -> bool:
    """Is the pgid still ALLOCATED? One syscall, and a narrow meaning.

    Note what this does NOT mean: a group whose every member is a zombie still
    answers yes, because the pgid stays allocated until the parent waits. Use
    ``group_has_live_member`` for "is anything still running in there".
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def group_members(pgid: int) -> tuple[psutil.Process, ...]:
    """Every live process whose process group is ``pgid``, leader included.

    psutil has no portable pgid attribute, so the group is resolved with
    ``os.getpgid`` per pid. Processes that exit mid-walk are skipped rather
    than raising: the walk is a snapshot of a moving target by definition.
    """
    found: list[psutil.Process] = []
    for proc in psutil.process_iter():
        try:
            if os.getpgid(proc.pid) == pgid:
                found.append(proc)
        except (ProcessLookupError, PermissionError, OSError, psutil.Error):
            continue
    return tuple(found)


def live_group_members(pgid: int) -> tuple[psutil.Process, ...]:
    """Group members that are not zombies.

    A zombie is a dead process whose parent has not waited on it yet. It keeps
    the pgid allocated and, on macOS, makes ``killpg`` answer EPERM -- which is
    how an uncaught PermissionError used to escape a reap. Nothing of it is
    still executing, so for cleanup purposes it does not count. A member whose
    status cannot be read IS counted: failing to inspect something is not
    evidence that it is dead.

    TWO GATES EXCLUDE A ZOMBIE, and which one fires is platform-dependent.
    Measured on darwin: ``psutil.process_iter`` DOES yield the corpse and
    ``psutil.Process(pid).status()`` DOES report "zombie", but ``os.getpgid``
    on it raises ProcessLookupError -- so ``group_members`` has already
    dropped it and the status check below never sees it. The status check is
    the gate that fires where getpgid still answers for a corpse. Do not
    "simplify" either one on the evidence of a single platform: the
    ProcessLookupError arm of ``group_members`` reads like defensive noise and
    is load-bearing here.
    """
    live: list[psutil.Process] = []
    for proc in group_members(pgid):
        try:
            if proc.status() == psutil.STATUS_ZOMBIE:
                continue
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            pass
        live.append(proc)
    return tuple(live)


def group_has_live_member(pgid: int) -> bool:
    """Is anything in the group still RUNNING (as opposed to merely present)?"""
    return bool(live_group_members(pgid))


def signal_group(pgid: int, sig: int) -> str | None:
    """Send ``sig``, or return the sentence explaining why there was no need.

    PermissionError is not an error path when the group holds only zombies:
    macOS refuses signals to a group with no live member, which is the very
    condition that means the work is already done. A group that DOES have live
    members and still refuses the signal is somebody else's, and that is a
    loud failure rather than a silent skip.
    """
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return f"process group {pgid} was already gone"
    except PermissionError as exc:
        if not group_has_live_member(pgid):
            return f"process group {pgid} holds only dead (zombie) members; there was nothing left to kill"
        raise WorkerCleanupError(
            f"not permitted to signal process group {pgid} ({exc}); it has live members this process does not own"
        ) from exc
    return None


def wait_group_gone(pgid: int, timeout_s: float) -> bool:
    """True once no LIVE process remains in the group.

    Two cadences on purpose: ``process_group_exists`` is one syscall and runs
    every poll, while the live-member walk enumerates every process on the box
    and runs on a much slower beat. Without the walk a zombie group never looks
    gone, so a ladder burned its full grace and then took EPERM on SIGKILL.
    """
    deadline = time.monotonic() + timeout_s
    next_live_check = 0.0
    while process_group_exists(pgid):
        now = time.monotonic()
        if now >= next_live_check:
            if not group_has_live_member(pgid):
                return True
            next_live_check = now + _LIVE_CHECK_EVERY_S
        if now >= deadline:
            return not group_has_live_member(pgid)
        time.sleep(_POLL_S)
    return True


#: POSIX has process groups to signal; Windows does not, and stops a tree with
#: ``taskkill /T`` instead. Callers spawn with ``start_new_session=GROUPS_SUPPORTED``
#: so a stopped child takes what it spawned with it.
GROUPS_SUPPORTED: bool = hasattr(os, "killpg")


def windows_taskkill(
    env: Mapping[str, str] = os.environ, *, exists: Callable[[str], bool] = os.path.isfile
) -> str | None:
    """Where Windows keeps ``taskkill.exe``, or None when it cannot be found there.

    Resolved under the system root the OS sets (``SystemRoot``, else ``windir``),
    never looked up on PATH: a sanitized service or CI PATH would hide it and a
    shadowing one could replace it (Codex on #6830). A root that is not absolute,
    or no file at that path, is None, never a guess.
    """
    root = next((env[key] for key in ("SystemRoot", "SYSTEMROOT", "windir", "WINDIR") if env.get(key)), None)
    if root is None or not ntpath.isabs(root):
        return None
    taskkill = ntpath.join(root, "System32", "taskkill.exe")
    return taskkill if exists(taskkill) else None


def tree_kill_argv(pid: int, *, platform: str, taskkill: str) -> list[str] | None:
    """The command that ends a whole process tree where process groups do not exist.

    On Windows ``terminate()`` reaches only the direct child, so a reloading
    server's worker, a node process or Electron's helpers would outlive it and
    keep their ports. ``taskkill /T`` walks the tree instead. POSIX returns
    None: the caller signals the process group.
    """
    if not platform.startswith("win"):
        return None
    return _taskkill_tree(pid, taskkill)


def _taskkill_tree(pid: int, taskkill: str) -> list[str]:
    return [taskkill, "/T", "/F", "/PID", str(pid)]


def stop_tree(
    pid: int,
    *,
    force: bool,
    kill_leader: Callable[[], None],
    groups: bool = GROUPS_SUPPORTED,
    platform: str = sys.platform,
    taskkill: Callable[[], str | None] = windows_taskkill,
) -> str | None:
    """Ask the tree ``pid`` leads to stop, on any platform; None, or why the tree was not reached.

    POSIX: signal the process group ``pid`` leads, SIGTERM or (``force``)
    SIGKILL. Windows: ``taskkill /T /F``, which has no polite form; with no
    ``taskkill.exe`` under the system root only the leader is killed. A
    platform with neither falls back to ``kill_leader``. A returned sentence
    is a cleanup failure, not a warning: a descendant the caller never
    snapshotted may still be running, and only the tree stop could reach it
    (Sol P1 on #6830).
    """
    if groups:
        sig = getattr(signal, "SIGKILL", signal.SIGTERM) if force else signal.SIGTERM
        with contextlib.suppress(ProcessLookupError):
            signal_group(pid, sig)
        return None
    if not platform.startswith("win"):
        kill_leader()
        return None
    resolved = taskkill()
    if resolved is None:
        kill_leader()
        return f"taskkill.exe not found under the system root; killed only process {pid}, not its tree"
    result = subprocess.run(_taskkill_tree(pid, resolved), check=False, capture_output=True)
    if result.returncode == 0:
        return None
    detail = (result.stdout + result.stderr).decode(errors="replace").strip() or "no output"
    return f"taskkill exited {result.returncode}: {detail}"


def group_alive(pgid: int, *, groups: bool = GROUPS_SUPPORTED) -> bool:
    """Whether a live member remains in group ``pgid``; always False without process groups."""
    return groups and group_has_live_member(pgid)


def group_gone_within(pgid: int, timeout_s: float, *, groups: bool = GROUPS_SUPPORTED) -> bool:
    """Whether group ``pgid`` has no live member within ``timeout_s``; always True without process groups.

    The wait matters after a SIGKILL: a member the caller never tracked can still be
    listed for the moment before the kernel schedules its death, so an instant check
    would report a kill that is in fact landing as one that failed (Codex on #6830).
    """
    return not groups or wait_group_gone(pgid, timeout_s)


def group_held_by(pgid: int, procs: Iterable[psutil.Process], *, groups: bool = GROUPS_SUPPORTED) -> bool:
    """Whether one of ``procs`` is still running in group ``pgid``; always False without process groups.

    After a group's leader is reaped, its id stays reserved only while a member
    holds it, so this is what proves the id still names the caller's group and
    not someone else's that reused the number. Each ``psutil.Process`` checks
    its own identity against its start time, so a reused pid never counts.
    """
    if not groups:
        return False
    for proc in procs:
        with contextlib.suppress(psutil.Error, OSError):
            if proc.is_running() and os.getpgid(proc.pid) == pgid and proc.is_running():
                return True
    return False


__all__ = [
    "GROUPS_SUPPORTED",
    "WorkerCleanupError",
    "group_alive",
    "group_gone_within",
    "group_has_live_member",
    "group_held_by",
    "group_members",
    "live_group_members",
    "process_group_exists",
    "signal_group",
    "stop_tree",
    "tree_kill_argv",
    "wait_group_gone",
    "windows_taskkill",
]
