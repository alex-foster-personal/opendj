"""Can this process READ that directory, and if not, why not.

macOS answers a TCC-blocked directory listing with an EMPTY listing, not an
error. ``stat()`` still succeeds, ``os.walk`` yields nothing, and a music
folder holding forty thousand tracks presents itself as a folder holding
none. Every "0 files" in this codebase therefore has to be able to say
which of the two it is, which is what :class:`AccessProbe` exists for.

The same shape shows up twice more in this tree and both are already
handled elsewhere: an evicted iCloud placeholder reads as a real file with
no bytes (:mod:`apps.shared.fs_residency`), and an offloaded directory
reads as silently empty. Permission is the third member of that family and
this is its module.

HONEST DENOMINATORS: a count taken while :func:`denied_roots` is non-empty
is a count of what we were allowed to see. It must be reported with that
list attached, never as the whole library.
"""

from __future__ import annotations

import dataclasses
import os
import threading
import time
from pathlib import Path

from apps.shared.platform_paths import HOME, MUSIC_ROOTS

#: How long one :func:`probe_all` call waits for its listings. A listing of
#: ~/Desktop, ~/Documents or ~/Music by a process macOS has not asked about
#: yet BLOCKS inside the kernel until the person answers the consent dialog,
#: and a listing on a dead network mount blocks for as long as the mount
#: does. Neither may hold an HTTP request open: /api/v1/setup/status runs
#: these probes, and the first-run overlay gives up on it after 60 s.
PROBE_DEADLINE_S: float = 2.0

#: Most listing threads that may be blocked at once across every caller with
#: a deadline. A blocked listing cannot be cancelled, so without a ceiling a
#: stream of requests naming distinct paths on a dead mount would pile up
#: threads until the process fails (Codex on #6825). A path over the ceiling
#: is answered as no-answer (``pending``) without starting a thread.
MAX_PROBE_THREADS: int = 256

#: Poll interval while waiting for sibling jobs to finish or for a stop Event.
_WAIT_SLICE_S: float = 0.05

#: Hang break only; matches ``tests.waits.THREAD_HANG_GUARD_S``.
_HANG_GUARD_S: float = 120.0

#: What a macOS operator has to do about a denied folder. Spelled once,
#: served over HTTP, rendered verbatim -- a UI that says "access denied"
#: without saying where to go is only half an answer.
GRANT_INSTRUCTIONS: str = (
    "macOS is blocking this app from reading the folder. Open System "
    "Settings > Privacy & Security > Files and Folders (or Full Disk "
    "Access), enable the app that runs this engine -- your terminal, or "
    "the packaged app -- and then restart it. macOS only asks once, so a "
    "dialog that was dismissed will not come back on its own."
)


@dataclasses.dataclass(frozen=True)
class AccessProbe:
    """One directory, and the four-state answer about reading it.

    ``exists`` and ``readable`` are separate answers because on macOS they
    disagree constantly. ``denied`` narrows an unreadable directory to the
    permission case specifically, because that is the only one an operator
    can fix from System Settings. ``pending`` marks the listing that gave no
    answer before the deadline, carried in the answer itself so a listing
    that finishes a moment later cannot change what this probe said.

    ``exists`` is true only when it was established. A pending probe whose
    path was never examined (past the thread ceiling, or still blocked on
    the existence check itself) says ``exists=False`` with ``pending=True``:
    not known yet, never claimed present (Sol on #6825).
    """

    path: str
    exists: bool
    readable: bool
    denied: bool
    detail: str
    pending: bool

    def to_dict(self) -> dict[str, object]:
        return dataclasses.asdict(self)


def probe_readable(path: Path) -> AccessProbe:
    """Try to LIST ``path``, because stat() is not the permission that matters.

    One directory entry is enough to prove the listing is allowed, and it is
    O(1) on a folder with forty thousand tracks in it.

    The PermissionError/OSError split is load-bearing: ``denied=True`` means
    TCC and has a fix, ``denied=False`` with ``readable=False`` means
    something else broke and the reason is passed through verbatim.
    """
    if not path.exists():
        return AccessProbe(
            path=str(path),
            exists=False,
            readable=False,
            denied=False,
            detail="not present on this machine",
            pending=False,
        )
    if not path.is_dir():
        return AccessProbe(
            path=str(path),
            exists=True,
            readable=False,
            denied=False,
            detail="exists but is not a directory",
            pending=False,
        )
    try:
        with os.scandir(path) as entries:
            next(iter(entries), None)
    except PermissionError as exc:
        return AccessProbe(
            path=str(path),
            exists=True,
            readable=False,
            denied=True,
            detail=f"macOS refused the listing ({exc.strerror or exc})",
            pending=False,
        )
    except OSError as exc:
        return AccessProbe(
            path=str(path),
            exists=True,
            readable=False,
            denied=False,
            detail=f"could not be listed: {exc.strerror or exc}",
            pending=False,
        )
    return AccessProbe(
        path=str(path),
        exists=True,
        readable=True,
        denied=False,
        detail="readable",
        pending=False,
    )


#: Folders under HOME worth offering as one-click setup suggestions, in
#: priority order. Not every entry exists on every machine - the caller
#: filters to what actually does.
CANDIDATE_MUSIC_FOLDERS: list[Path] = [
    HOME / "Music",
    HOME / "Music" / "rekordbox",
    HOME / "Music" / "Music" / "Media.localized",
]


def music_folder_candidates() -> list[AccessProbe]:
    """Existing folders under HOME worth offering as setup suggestions.

    Filtered to ``probe.exists`` only - a path that is not on this machine
    is not a candidate, never a guess rendered as one. A candidate that
    DOES exist but cannot be read (macOS TCC denial) is still returned,
    with ``readable=False`` / ``denied=True`` / ``detail`` set, so the caller
    can show it as refused rather than silently drop it.
    """
    probes = probe_all(CANDIDATE_MUSIC_FOLDERS)
    return [probe for probe in probes if probe.exists]


def music_root_access() -> list[AccessProbe]:
    """Probe every folder a library is expected to live under.

    ``MUSIC_ROOTS`` is the configured answer; the three macOS folders that
    carry their own TCC prompt are added because a library pointing into
    ~/Desktop or ~/Documents hits exactly the same wall, and reporting only
    ~/Music would miss it. Order is stable and duplicates are dropped.
    """
    candidates: list[Path] = list(MUSIC_ROOTS)
    candidates += [HOME / "Music", HOME / "Desktop", HOME / "Documents"]
    return probe_all(candidates)


class _InFlight:
    """One listing running on its own thread, and its answer once it has one."""

    def __init__(self, path: Path) -> None:
        self.result: AccessProbe | None = None
        #: Set once the existence check returns, before the listing that may block.
        self.present: bool | None = None
        self.thread = threading.Thread(
            target=self._run,
            args=(path,),
            name=f"fs-access-probe:{path}",
            daemon=True,
        )

    def _run(self, path: Path) -> None:
        try:
            self.present = path.exists()
            self.result = probe_readable(path)
        finally:
            # Done: drop the map's entry now rather than when the same path is
            # asked about again. Callers still hold this job directly (Sol on #6825).
            with _IN_FLIGHT_LOCK:
                if _IN_FLIGHT.get(path) is self:
                    del _IN_FLIGHT[path]


#: Listings still blocked from an earlier call, by path. A blocked listing
#: cannot be cancelled from Python, so the next call waits on the SAME one
#: rather than starting another: at most one thread per path is ever stuck,
#: and a dialog answered in between is picked up on the next call. The
#: threads are daemons so a wedged one never holds engine shutdown (#766).
_IN_FLIGHT: dict[Path, _InFlight] = {}
_IN_FLIGHT_LOCK = threading.Lock()


def _no_answer(path: Path, deadline_s: float, *, present: bool | None) -> AccessProbe:
    """The UNKNOWN answer: not readable, not proven denied, and why.

    ``present`` is what the listing thread established before it blocked;
    None (never examined) reports ``exists=False``, never a guessed True.
    """
    if present is None:
        return AccessProbe(
            path=str(path),
            exists=False,
            readable=False,
            denied=False,
            pending=True,
            detail=(
                f"not checked within {deadline_s:g} s: other folders are still "
                "waiting on macOS or on a volume that is not responding. Check again."
            ),
        )
    return AccessProbe(
        path=str(path),
        exists=present,
        readable=False,
        denied=False,
        pending=True,
        detail=(
            f"no answer within {deadline_s:g} s: macOS may be waiting on a "
            "permission dialog for this folder, or the volume is not "
            "responding. Answer the dialog, then check again."
        ),
    )


def probe_all(
    paths: list[Path],
    *,
    deadline_s: float | None = PROBE_DEADLINE_S,
    max_threads: int = MAX_PROBE_THREADS,
    give_up: threading.Event | None = None,
) -> list[AccessProbe]:
    """Probe each path once, in order, with duplicates dropped.

    The listings run side by side and the whole call returns within about
    ``deadline_s``. A listing that has not answered by then is reported with
    :func:`_no_answer`, never as readable and never as denied.

    ``deadline_s=None`` waits for every answer. An import passes it: a root
    with no answer yet is neither readable nor denied, so a deadline there
    would drop it from the walk and report a smaller library with no error.

    With a deadline, at most ``max_threads`` listings are alive at once across
    every caller; a path past that ceiling is reported no-answer without a
    thread. An import (no deadline) is not capped: it needs every answer.

    ``give_up`` is a test seam; omit it outside tests. When omitted and
    ``deadline_s`` is not None, an internal Event is armed with
    ``threading.Timer(deadline_s, event.set)`` at entry (before the lock
    acquire), so waiting on the lock spends the same budget as the listings
    (Sol on #6825). When the caller passes ``give_up``, that Event is the sole
    wait signal; ``deadline_s`` still names the budget (detail text, thread
    ceiling).
    """
    stop: threading.Event | None = None
    timer: threading.Timer | None = None
    if deadline_s is not None:
        stop = give_up if give_up is not None else threading.Event()
        if give_up is None:
            timer = threading.Timer(deadline_s, stop.set)
            timer.start()
    try:
        ordered = _unique(paths)
        running = _start(ordered, stop=stop, max_threads=max_threads)
        _wait(running, stop)
        return _collect(ordered, running, deadline_s, stop)
    finally:
        if timer is not None:
            timer.cancel()


def _unique(paths: list[Path]) -> list[Path]:
    """The expanded paths in order, each once."""
    seen: set[Path] = set()
    ordered: list[Path] = []
    for candidate in paths:
        resolved = candidate.expanduser()
        if resolved not in seen:
            seen.add(resolved)
            ordered.append(resolved)
    return ordered


def _start(ordered: list[Path], *, stop: threading.Event | None, max_threads: int) -> list[_InFlight | None]:
    """Reuse or start one listing per path; None for a path past the thread ceiling or the deadline."""
    if stop is None:
        _IN_FLIGHT_LOCK.acquire()
        acquired = True
    else:
        acquired = False
        started = time.monotonic()
        while True:
            if stop.is_set():
                return [None] * len(ordered)
            if _IN_FLIGHT_LOCK.acquire(timeout=_WAIT_SLICE_S):
                acquired = True
                break
            if time.monotonic() - started > _HANG_GUARD_S:
                return [None] * len(ordered)
    running: list[_InFlight | None] = []
    try:
        alive = len(_IN_FLIGHT)  # finished listings remove themselves
        for path in ordered:
            if stop is not None and stop.is_set():
                running.append(None)
                continue
            job = _IN_FLIGHT.get(path)
            if job is None:
                if stop is not None and alive >= max_threads:
                    running.append(None)
                    continue
                job = _InFlight(path)
                _IN_FLIGHT[path] = job
                job.thread.start()
                alive += 1
            running.append(job)
    finally:
        if acquired:
            _IN_FLIGHT_LOCK.release()
    return running


def _wait(running: list[_InFlight | None], stop: threading.Event | None) -> None:
    """Join every listing, or return when stop fires or every job has answered."""
    if stop is None:
        for job in running:
            if job is not None:
                job.thread.join()
        return
    started = time.monotonic()
    while True:
        if stop.is_set():
            return
        if not any(job is not None and job.thread.is_alive() for job in running):
            return
        if time.monotonic() - started > _HANG_GUARD_S:
            return
        stop.wait(timeout=_WAIT_SLICE_S)


def _collect(
    ordered: list[Path], running: list[_InFlight | None], deadline_s: float | None, stop: threading.Event | None
) -> list[AccessProbe]:
    """Each path's answer, or the no-answer probe for one still blocked or never started."""
    probes: list[AccessProbe] = []
    for path, job in zip(ordered, running, strict=True):
        if job is None:
            probes.append(_no_answer(path, deadline_s or 0.0, present=None))
            continue
        if job.result is not None:
            probes.append(job.result)
            continue
        if stop is not None and job.thread.is_alive():
            probes.append(_no_answer(path, deadline_s or 0.0, present=job.present))
            continue
        raise RuntimeError(f"the access probe for {path} ended without an answer")
    return probes


def denied_roots(probes: list[AccessProbe]) -> list[str]:
    """Just the paths macOS refused. The honest-denominator caveat list."""
    return [probe.path for probe in probes if probe.denied]


def pending_roots(probes: list[AccessProbe]) -> list[str]:
    """The paths that gave no answer in time: on a consent dialog, or a mount not answering.

    Neither readable nor denied yet. An import may start on them, because the
    import itself waits for every answer (``probe_all(..., deadline_s=None)``).
    Read from the probes, never from the live listing threads: a listing that
    answers between :func:`probe_all` returning and this call must not turn
    a no-answer probe into a refusal (Codex on #6825).
    """
    return [probe.path for probe in probes if probe.pending]


def unreadable_roots(probes: list[AccessProbe]) -> list[str]:
    """Every path that could not be listed: refused, broken, or no answer yet.

    A superset of :func:`denied_roots`. A listing still on its consent dialog
    is neither readable nor denied, and must not count as readable either,
    whether or not its existence was established before the deadline.
    """
    return [probe.path for probe in probes if probe.pending or (probe.exists and not probe.readable)]


__all__ = [
    "CANDIDATE_MUSIC_FOLDERS",
    "GRANT_INSTRUCTIONS",
    "MAX_PROBE_THREADS",
    "PROBE_DEADLINE_S",
    "AccessProbe",
    "denied_roots",
    "music_folder_candidates",
    "music_root_access",
    "pending_roots",
    "probe_all",
    "probe_readable",
    "unreadable_roots",
]
