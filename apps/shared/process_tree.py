"""Start a child that owns its process tree, stop the whole tree, prove it gone.

One seam for one OS concern (ADR-NEW-portable-by-default-not-a-rust-rewrite).
Callers used to carry their own copy of this: ``killpg`` ladders that raise
AttributeError on Windows, a ``taskkill /T /F`` arm that is forced from the
first instant and proves nothing afterwards, and an explicit "POSIX-only"
refusal. Each copy was right on one OS and wrong on the other.

The tree is the thing a caller owns, not the pid it holds a handle to:

- POSIX: the child leads a new session, so its PROCESS GROUP is the tree. The
  group primitives (zombie-aware liveness, the signal that tolerates an
  all-zombie group, the two-cadence wait) live in ``apps.shared.process_groups``
  and this module is their only ladder.
- Windows: there is no process group a non-console API can enumerate, so the
  tree is the root plus everything reachable from it by parentage. A dead
  parent's children keep their (now stale) ppid on Windows, so a tree whose
  root already exited is still found. Every parent link is checked against
  start times so a recycled pid cannot pull a stranger into the tree. The
  polite rung is CTRL_BREAK_EVENT to the root's own console group, sent ONLY
  for a root this module spawned with CREATE_NEW_PROCESS_GROUP and still holds:
  aimed at a pid that does not lead its own group, the same call reaches the
  whole console, the caller included (the trap PR #6913 closed for signal 0).

Known limit, Windows: a grandchild whose own parent died is reachable only
through that dead parent's pid, so a tree with a dead INTERMEDIATE loses the
branch below it. A Job Object would close that gap and is not built here.
Note that a venv's ``python.exe`` on Windows is a launcher that runs the real
interpreter as its own child, so every venv-launched Python is two processes
(measured on bifrost1: killing uv's launcher does end that interpreter, but a
child the interpreter started outlives a kill of its parent). The parentage
walk is what reaches those children.

Liveness of a single pid goes through ``apps.shared.pid_liveness``; identity
(was this pid recycled?) through psutil's start time.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
import weakref
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import psutil

from apps.shared.pid_liveness import pid_is_running
from apps.shared.process_groups import (
    CREATE_TIME_TOLERANCE_S,
    WorkerCleanupError,
    live_group_members,
    signal_group,
    wait_group_gone,
)

_WINDOWS = sys.platform == "win32"

#: Seconds of every stop held back for the forced rung and its confirm. A kill
#: is not instantaneous either, so spending the whole grace on the polite rung
#: would turn every stubborn tree into a raise instead of a kill. Capped at half
#: the grace so a small grace still gets a polite rung.
KILL_CONFIRM_S: float = 2.0
#: Seconds between looks while waiting for a Windows tree to empty out.
_POLL_S: float = 0.05
#: Popen keywords that decide the child's session or group; spawn_tree owns them.
_GROUP_FLAGS: tuple[str, ...] = ("start_new_session", "creationflags", "process_group")


class ProcessTreeError(WorkerCleanupError):
    """The tree could not be stopped, or could not be proven gone.

    A WorkerCleanupError subclass so every caller that already catches the
    shared cleanup failure keeps catching this one.
    """


@dataclass(frozen=True)
class TreeRoot:
    """What makes a later stop provable: the root pid and when it started.

    ``console_group`` is True only for a Windows root spawned by
    :func:`spawn_tree`, which leads its own console group. Anything else gets
    no CTRL_BREAK, because that signal is only safe aimed at a group leader.

    ``pinned`` is True when this process holds the root's spawn handle. On
    Windows an open handle stops the pid being reused, so a process naming a
    DEAD pinned root as its parent is provably the root's child. A dead root
    nobody pins may have had its pid reused by a stranger that spawned and
    died too, so its orphans are never adopted (see unattributable_orphans).
    """

    pid: int
    started_at: float
    console_group: bool
    pinned: bool


#: The root of every handle spawn_tree returned, for as long as the handle lives.
_SPAWNED: weakref.WeakKeyDictionary[subprocess.Popen[Any], TreeRoot] = weakref.WeakKeyDictionary()


@dataclass(frozen=True)
class TreeStopOutcome:
    """What a stop did. ``forced`` says whether the forced rung was needed."""

    sentence: str
    forced: bool


def _owner_popen_kwargs() -> dict[str, Any]:
    """Popen keywords that make the child own a tree this module can stop."""
    if _WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def spawn_tree(
    argv: Sequence[str], **popen_kwargs: Any
) -> tuple[subprocess.Popen[Any], TreeRoot]:
    """Start ``argv`` as the root of a tree it owns; return the handle and root."""
    clashing = sorted(set(popen_kwargs) & set(_GROUP_FLAGS))
    if clashing:
        raise ProcessTreeError(
            f"spawn_tree sets the child's session and group itself; refused the "
            f"caller-supplied {', '.join(clashing)}"
        )
    proc = subprocess.Popen(list(argv), **popen_kwargs, **_owner_popen_kwargs())
    root = TreeRoot(
        pid=proc.pid, started_at=_start_time(proc.pid), console_group=_WINDOWS, pinned=True
    )
    _SPAWNED[proc] = root
    return proc, root


def root_of(proc: subprocess.Popen[Any]) -> TreeRoot:
    """The root :func:`spawn_tree` recorded for ``proc``; refuses any other handle.

    For a caller that hands the Popen on and stops it somewhere else. A handle
    started any other way may not lead its own group, so it gets no root.
    """
    try:
        return _SPAWNED[proc]
    except KeyError:
        raise ProcessTreeError(
            f"pid {proc.pid} was not started by spawn_tree, so nothing proves it owns its tree"
        ) from None


def root_for_group_leader(pid: int, started_at: float) -> TreeRoot:
    """A root recorded earlier (a jobs row): no console group to signal, no handle pinning it."""
    if pid <= 1:
        raise ProcessTreeError(
            f"pid {pid} is not a killable tree root (0 would name the caller's own group)"
        )
    return TreeRoot(pid=pid, started_at=started_at, console_group=False, pinned=False)


def _start_time(pid: int) -> float:
    """psutil's start time, or now for a child that already exited.

    A child can exit (and, on macOS, stop answering as a zombie) before this
    read; the wall clock is taken within milliseconds of the spawn, so it stays
    inside CREATE_TIME_TOLERANCE_S of the true start. A child we just spawned
    that is live but unreadable is not explainable, so that raises.
    """
    try:
        return psutil.Process(pid).create_time()
    except psutil.NoSuchProcess:
        return time.time()
    except psutil.AccessDenied as exc:
        raise ProcessTreeError(f"the child we just spawned (pid {pid}) is not inspectable: {exc}") from exc


def _root_is_ours(root: TreeRoot) -> bool:
    """True when the root pid is live AND still the process we started.

    Raises when the pid is live but started at another time: that pid was
    recycled, the tree belongs to a stranger, and nothing may be signalled.
    """
    if not pid_is_running(root.pid):
        return False
    try:
        created = psutil.Process(root.pid).create_time()
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied as exc:
        raise ProcessTreeError(
            f"root pid {root.pid} is live but not inspectable ({exc}); cannot prove it is ours"
        ) from exc
    drift = abs(created - root.started_at)
    if drift > CREATE_TIME_TOLERANCE_S:
        raise ProcessTreeError(
            f"root pid {root.pid} started {drift:.1f}s away from the recorded "
            "start; the pid was recycled, so its tree is not ours"
        )
    return True


def live_tree_members(root: TreeRoot) -> tuple[psutil.Process, ...]:
    """Every process of the tree still running, root included when it lives."""
    if not _WINDOWS:
        return live_group_members(root.pid)
    return _windows_tree_members(root, trust_dead_root=root.pinned)


def unattributable_orphans(root: TreeRoot) -> tuple[psutil.Process, ...]:
    """Processes naming a dead, unpinned root (or its descendants) that cannot be proven ours.

    Windows only. They would be the root's orphans, or the children of a
    stranger that reused the root's pid and died too; nothing can tell the
    two apart without a handle or a Job Object, so they are reported, never
    stopped. On POSIX a pgid is not reused while its group has a member, so
    there is no such case and this is always empty.
    """
    if not _WINDOWS or root.pinned:
        return ()
    attributable = {member.pid for member in _windows_tree_members(root, trust_dead_root=False)}
    every_link = _windows_tree_members(root, trust_dead_root=True)
    return tuple(proc for proc in every_link if proc.pid not in attributable)


@dataclass(frozen=True)
class ProcessRow:
    """One process-table row: what the parentage rule needs and nothing else."""

    pid: int
    ppid: int
    started: float


def tree_pids_by_parentage(
    root_pid: int, root_started: float, rows: Sequence[ProcessRow], *, root_pinned: bool
) -> set[int]:
    """Pids of the tree rooted at ``root_pid``, from one process-table snapshot.

    A dead parent's children keep its pid as their ppid on Windows, so a tree
    whose root exited is still found. Two guards keep a recycled pid from
    pulling a stranger in: a child cannot be older than its parent, and once a
    stranger holds the parent's pid, a child born after the stranger started is
    the stranger's. Neither guard can see a stranger that took a dead root's
    pid and then died itself, so a dead root's children are walked only when
    ``root_pinned`` says nothing could have reused that pid.
    """
    started = {row.pid: row.started for row in rows}
    by_parent: dict[int, list[ProcessRow]] = {}
    for row in rows:
        if row.pid != row.ppid:
            by_parent.setdefault(row.ppid, []).append(row)
    found: set[int] = set()
    holder = started.get(root_pid)
    if holder is not None and abs(holder - root_started) <= CREATE_TIME_TOLERANCE_S:
        found.add(root_pid)
    elif not root_pinned:
        return found  # dead (or recycled) and unpinned: nothing naming it is provably ours
    frontier = [(root_pid, root_started)]
    while frontier:
        parent_pid, parent_start = frontier.pop()
        holder = started.get(parent_pid)
        stranger_since = (
            holder
            if holder is not None and abs(holder - parent_start) > CREATE_TIME_TOLERANCE_S
            else None
        )
        for child in by_parent.get(parent_pid, []):
            if child.started < parent_start - CREATE_TIME_TOLERANCE_S:
                continue  # older than our parent: a child of an earlier holder of that pid
            if stranger_since is not None and child.started >= stranger_since:
                continue  # born after a stranger took the pid: the stranger's child
            if child.pid not in found:
                found.add(child.pid)
                frontier.append((child.pid, child.started))
    return found


def _windows_tree_members(root: TreeRoot, *, trust_dead_root: bool) -> tuple[psutil.Process, ...]:
    """The live tree, reading start times only for processes linked to the root.

    ``psutil.process_iter`` costs seconds on Windows (measured 5.5 s for 433
    processes on bifrost1, ``ppid`` alone), because it reads every process's
    start time. psutil's parent map is one OS snapshot (under 1 ms), so the
    walk finds candidates by pid links first and reads start times for those
    alone. That map is what ``psutil.Process.children`` itself uses.
    """
    parent_of = _parent_map()
    children_of: dict[int, list[int]] = {}
    for pid, ppid in parent_of.items():
        if pid != ppid:
            children_of.setdefault(ppid, []).append(pid)
    linked = {root.pid}
    frontier = [root.pid]
    while frontier:
        for child in children_of.get(frontier.pop(), []):
            if child not in linked:
                linked.add(child)
                frontier.append(child)
    snapshot: dict[int, psutil.Process] = {}
    rows: list[ProcessRow] = []
    for pid in linked:
        if pid not in parent_of:
            continue  # a dead link: the root itself, or a pid that exited mid-walk
        try:
            proc = psutil.Process(pid)
            started = proc.create_time()
            if proc.ppid() != parent_of[pid]:
                continue  # exited and reused between the map and this read: another process
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as exc:
            raise ProcessTreeError(f"process {pid}, linked to tree {root.pid}, is not inspectable: {exc}") from exc
        snapshot[pid] = proc
        rows.append(ProcessRow(pid=pid, ppid=parent_of[pid], started=started))
    pids = tree_pids_by_parentage(root.pid, root.started_at, rows, root_pinned=trust_dead_root)
    return tuple(snapshot[pid] for pid in sorted(pids))


def _parent_map() -> dict[int, int]:
    """pid -> ppid for every process, from psutil's own one-snapshot map."""
    ppid_map = getattr(psutil, "_ppid_map", None)
    if ppid_map is None:
        raise ProcessTreeError(
            f"psutil {psutil.__version__} has no _ppid_map; the Windows tree walk "
            "needs it (it is what psutil.Process.children uses) and must be ported"
        )
    return dict(ppid_map())


def stop_tree(root: TreeRoot, *, grace_s: float) -> TreeStopOutcome:
    """Polite signal, then forced after the grace; return once every member is gone.

    The whole ladder fits inside ONE ``grace_s``: the polite rung gets what is
    left after reserving :data:`KILL_CONFIRM_S` (capped at half the grace) for
    the forced rung and its confirm. Raises :class:`ProcessTreeError` when the
    root pid was recycled or any member outlives the forced rung.
    """
    if grace_s <= 0:
        raise ValueError(f"grace_s must be > 0, got {grace_s}")
    deadline = time.monotonic() + grace_s
    kill_at = deadline - min(KILL_CONFIRM_S, grace_s / 2)
    try:
        root_alive = _root_is_ours(root)
        if _WINDOWS:
            return _stop_windows_tree(root, root_alive, kill_at, deadline)
        return _stop_posix_group(root.pid, kill_at, deadline)
    except psutil.Error as exc:
        raise ProcessTreeError(f"process tree {root.pid} could not be stopped: {exc!r}") from exc


def _stop_posix_group(pgid: int, kill_at: float, deadline: float) -> TreeStopOutcome:
    already = signal_group(pgid, signal.SIGTERM)
    if already is not None:
        return TreeStopOutcome(already, forced=False)
    if wait_group_gone(pgid, max(kill_at - time.monotonic(), 0.0)):
        return TreeStopOutcome(f"process group {pgid} exited on SIGTERM", forced=False)
    already = signal_group(pgid, signal.SIGKILL)
    if already is not None:
        return TreeStopOutcome(already, forced=False)
    if not wait_group_gone(pgid, max(deadline - time.monotonic(), 0.0)):
        raise ProcessTreeError(f"process group {pgid} survived SIGKILL")
    return TreeStopOutcome(f"process group {pgid} killed after SIGTERM", forced=True)


def _stop_windows_tree(
    root: TreeRoot, root_alive: bool, kill_at: float, deadline: float
) -> TreeStopOutcome:
    if not live_tree_members(root):
        return TreeStopOutcome(f"process tree {root.pid} was already gone", forced=False)
    if not root.console_group:
        why = "its root has no console group of its own, so Windows has no polite signal for it"
    elif not root_alive:
        why = "its root already exited, so CTRL_BREAK has no group leader to reach"
    else:
        try:
            os.kill(root.pid, signal.CTRL_BREAK_EVENT)
        except OSError as exc:
            why = f"CTRL_BREAK could not be delivered ({exc})"
        else:
            if _wait_tree_gone(root, kill_at):
                return TreeStopOutcome(
                    f"process tree {root.pid} exited on CTRL_BREAK", forced=False
                )
            why = "it outlived CTRL_BREAK"
    _kill_tree_until(root, deadline)
    return TreeStopOutcome(f"process tree {root.pid} forced because {why}", forced=True)


def _wait_tree_gone(root: TreeRoot, until: float) -> bool:
    while live_tree_members(root):
        if time.monotonic() >= until:
            return False
        time.sleep(_POLL_S)
    return True


def _kill_tree_until(root: TreeRoot, deadline: float) -> None:
    """Kill every member, looking again for late forks, until none is left."""
    while True:
        members = live_tree_members(root)
        if not members:
            return
        if time.monotonic() >= deadline:
            raise ProcessTreeError(
                f"process tree {root.pid} still has live members "
                f"{[member.pid for member in members]} after the forced rung"
            )
        for member in members:
            _send(member, kill=True)
        psutil.wait_procs(members, timeout=_POLL_S)


def stop_processes(
    procs: Sequence[psutil.Process], *, grace_s: float
) -> list[psutil.Process]:
    """Terminate unrelated processes, kill the holdouts, return what still runs.

    For processes that share no tree (a parent's descendants found by
    parentage, a census's orphans). The grace is split as in :func:`stop_tree`.
    A returned process is one this call could not end, never one it skipped.
    """
    if grace_s <= 0:
        raise ValueError(f"grace_s must be > 0, got {grace_s}")
    confirm_s = min(KILL_CONFIRM_S, grace_s / 2)
    denied = [proc for proc in procs if not _send_or_denied(proc, kill=False)]
    _, alive = psutil.wait_procs(procs, timeout=grace_s - confirm_s)
    denied += [proc for proc in alive if not _send_or_denied(proc, kill=True)]
    _, alive = psutil.wait_procs(alive, timeout=confirm_s)
    still = [proc for proc in alive if _still_running(proc)]
    return still + [proc for proc in denied if proc not in still and _still_running(proc)]


def _send(proc: psutil.Process, *, kill: bool) -> None:
    """Signal one process; a process that is already gone needs nothing."""
    with contextlib.suppress(psutil.NoSuchProcess):
        proc.kill() if kill else proc.terminate()


def _send_or_denied(proc: psutil.Process, *, kill: bool) -> bool:
    """Signal one process; False when the OS refused (it is reported as a survivor)."""
    try:
        _send(proc, kill=kill)
    except psutil.AccessDenied:
        return False
    return True


def _still_running(proc: psutil.Process) -> bool:
    """Live, the same process (psutil checks its start time), and not a zombie."""
    if not pid_is_running(proc.pid) or not proc.is_running():
        return False
    try:
        return proc.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


__all__ = [
    "KILL_CONFIRM_S",
    "ProcessRow",
    "ProcessTreeError",
    "TreeRoot",
    "TreeStopOutcome",
    "live_tree_members",
    "root_for_group_leader",
    "root_of",
    "spawn_tree",
    "stop_processes",
    "stop_tree",
    "tree_pids_by_parentage",
    "unattributable_orphans",
]
