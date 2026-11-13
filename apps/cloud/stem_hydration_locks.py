"""Cross-process hydrate locks, publish generations and owner markers (ADR-0024).

Split from :mod:`apps.cloud.stem_hydration` (issue #9726), which re-exports
every name here.
"""

from __future__ import annotations

import contextlib
import glob
import json
import logging
import math
import os
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Literal

import psutil

from apps.cloud.eviction import HydrationError
from apps.cloud.stem_upload_queue import exclusive_file_lock, exclusive_file_lock_within
from apps.stems.artifacts import validate_stable_id
from apps.stems.bundle_lock import BUNDLE_LOCK_STRIPES, bundle_lock_path

#: Infix of a fetch's temp directory (``<stable_id>.tmp-hydrate-<pid>-<token>-*``)
#: while it is in flight; a progress read lists that directory by this name.
IN_FLIGHT_MARKER: str = ".tmp-hydrate-"

_hydrate_locks_guard = threading.Lock()
_hydrate_locks: dict[str, threading.Lock] = {}


def _hydrate_lock(stable_id: str) -> threading.Lock:
    """One lock per stable_id, created lazily, shared process-wide, so
    concurrent in-process hydration calls for the SAME bundle serialize
    instead of racing on the same destination directory."""
    with _hydrate_locks_guard:
        return _hydrate_locks.setdefault(stable_id, threading.Lock())


#: Stripes of the cross-process hydrate lock, the bundle lock's own stripes.
#: Bounded so the lock files never grow with the library; two ids sharing a
#: stripe only wait on each other.
HYDRATE_LOCK_STRIPES: int = BUNDLE_LOCK_STRIPES


def hydrate_lock_path(data_dir: Path, stable_id: str) -> Path:
    """The OS lock file a hydrate of ``stable_id`` holds across its whole fetch, in every process.

    Only hydrations take it, so two hydrations of one id never fetch at once.
    It is NOT the bundle writer lock (:func:`apps.stems.bundle_lock.bundle_lock_path`):
    a hydrate takes that one only around its publish, because a stems job's
    publish or a recompress swap of an unrelated id on the same stripe would
    otherwise wait out a whole R2 transfer (Codex on #6821).
    """
    writer_lock = bundle_lock_path(data_dir, stable_id)
    return writer_lock.with_name(f"fetch-{writer_lock.name}")


@contextlib.contextmanager
def _hydrate_locks_held(data_dir: Path, stable_id: str) -> Iterator[None]:
    """The in-process lock, then the cross-process one. The engine and a
    ``hydrate_runner`` job can hydrate the same id at once; with only the
    in-process lock both could judge an old bundle invalid and each remove
    the other's freshly published copy (Codex P1 on #4974)."""
    with _hydrate_lock(stable_id), exclusive_file_lock(hydrate_lock_path(data_dir, stable_id)):
        yield


@contextlib.contextmanager
def hydrate_locks_within(data_dir: Path, stable_id: str, timeout_s: float) -> Iterator[bool]:
    """Take the locks a hydrate of ``stable_id`` holds, waiting at most ``timeout_s``.

    Yields whether both were taken; ``timeout_s=0`` tries once. While they are
    held no hydrator, in this process or another, can be removing or
    publishing the bundle, so a read made under them sees a settled bundle.
    """
    deadline = time.monotonic() + timeout_s
    in_process = _hydrate_lock(stable_id)
    if not in_process.acquire(timeout=timeout_s):
        yield False
        return
    try:
        remaining = max(0.0, deadline - time.monotonic())
        with exclusive_file_lock_within(hydrate_lock_path(data_dir, stable_id), remaining) as taken:
            yield taken
    finally:
        in_process.release()


#: Logged under the facade module's name, so log filters and tests that
#: watch ``apps.cloud.stem_hydration`` see the same records after the split.
_log = logging.getLogger("apps.cloud.stem_hydration")


#: Windows refuses to replace or remove a file another process has open (an
#: antivirus scan, an indexer), raising PermissionError; it clears in moments.
_SHARING_RETRIES: int = 5
_SHARING_RETRY_S: float = 0.02


def _retry_sharing_violation(action: Callable[[], None]) -> None:
    """Run a replace or unlink, retrying a few times on PermissionError; the
    last PermissionError is raised to the caller."""
    for attempt in range(_SHARING_RETRIES):
        try:
            action()
        except PermissionError:
            if attempt == _SHARING_RETRIES - 1:
                raise
            time.sleep(_SHARING_RETRY_S)
        else:
            return


def _read_text_retrying(path: Path) -> str:
    """``path``'s text, retrying a Windows sharing violation like a replace."""
    text: list[str] = []
    _retry_sharing_violation(lambda: text.append(path.read_text(encoding="utf-8")))
    return text[0]


def _replace_or_raise(staged: Path, target: Path) -> None:
    """Atomically replace ``target`` with ``staged``, or raise HydrationError."""
    try:
        _retry_sharing_violation(lambda: os.replace(staged, target))
    except OSError as exc:
        _unlink_or_log(staged)
        raise HydrationError(f"could not write {target}: {exc}") from exc


def _unlink_or_log(path: Path) -> bool:
    """Remove ``path`` if present; a removal that keeps failing is logged and
    reported as ``False``, never raised over the caller's own result."""
    try:
        _retry_sharing_violation(lambda: path.unlink(missing_ok=True))
    except OSError as exc:
        _log.warning("stem-hydration: could not remove %s: %s", path, exc)
        return False
    return True


#: Owner markers this process finished with but could neither remove nor mark
#: done (see :func:`_owner_marker_done`). Their pid is this live process, so
#: they would otherwise read as a running fetch here.
_ABANDONED_MARKERS: set[Path] = set()
_abandoned_lock = threading.Lock()


def _done_sibling(marker: Path) -> Path:
    """``<marker>.done``: present means the marker's owner has finished."""
    return marker.with_name(f"{marker.name}.done")


def _finish_owner_marker(marker: Path) -> None:
    """Remove a finished owner's marker; if Windows refuses, record it done.

    A marker that cannot be removed still names its owner's live pid, so it is
    marked done with a NEW sibling file (an exclusive create, never a replace
    of the marker another process may hold open) that every process honours.
    Only if that is refused too does this process alone remember it.
    """
    if _unlink_or_log(marker):
        return
    try:
        _retry_sharing_violation(lambda: _create_done_sibling(_done_sibling(marker)))
    except OSError as exc:
        _log.warning("stem-hydration: could not mark %s done: %s", marker, exc)
        with _abandoned_lock:
            _ABANDONED_MARKERS.add(marker)


def _create_done_sibling(done: Path) -> None:
    """Create ``done`` exclusively; one already there is this owner's own record."""
    try:
        with done.open("x", encoding="utf-8", newline="\n"):
            pass
    except FileExistsError:
        pass


def _owner_marker_done(marker: Path) -> bool:
    """Whether ``marker``'s owner has finished, by its ``.done`` sibling or this
    process's own record; a finished marker is removed (marker first, then its
    ``.done``, so it never reads as owned again) when Windows allows."""
    with _abandoned_lock:
        abandoned = marker in _ABANDONED_MARKERS
    done = _done_sibling(marker)
    if not abandoned and not done.exists():
        return False
    if _unlink_or_log(marker) and _unlink_or_log(done):
        with _abandoned_lock:
            _ABANDONED_MARKERS.discard(marker)
    return True


def hydrate_generation_path(data_dir: Path, stable_id: str) -> Path:
    """The publish generation of ``stable_id``'s lock stripe, beside its lock file.

    Odd while a hydrate on the stripe is replacing a bundle (from just before
    it removes the old directory until its new one is verified or cleaned up),
    even otherwise. A reader that sees the same even value before and after a
    read knows no publish on the stripe overlapped it (a seqlock).
    """
    return hydrate_lock_path(data_dir, stable_id).with_suffix(".gen")


def read_hydrate_generation(data_dir: Path, stable_id: str) -> int:
    """The stripe's publish generation (0 before any publish). A read Windows
    keeps refusing raises HydrationError: a hydrate reports it as an error
    outcome, a state read treats the stripe as unsettled."""
    path = hydrate_generation_path(data_dir, stable_id)
    try:
        text = _read_text_retrying(path)
    except FileNotFoundError:
        return 0
    except OSError as exc:
        raise HydrationError(f"could not read {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise HydrationError(f"{path} is not text: {exc}") from exc
    try:
        return int(text)
    except ValueError as exc:
        raise HydrationError(f"{path} does not hold a generation: {exc}") from exc


def _write_hydrate_generation(data_dir: Path, stable_id: str, value: int) -> None:
    path = hydrate_generation_path(data_dir, stable_id)
    staged = path.with_name(f"{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text(str(value), encoding="utf-8", newline="\n")
    except OSError as exc:
        # Every failure here is a HydrationError, so the publish window's
        # closing write can always be handled by its own except clause.
        _unlink_or_log(staged)
        raise HydrationError(f"could not write {path}: {exc}") from exc
    _replace_or_raise(staged, path)  # readers never see a half-written value


@contextlib.contextmanager
def bundle_publish_window(data_dir: Path, stable_id: str) -> Iterator[None]:
    """Mark the stripe's generation odd while a bundle is being replaced.

    Call only while holding ``stable_id``'s hydrate locks, which serialize
    every writer of this stripe's generation.
    """
    start = read_hydrate_generation(data_dir, stable_id)
    opening = start + 1 if start % 2 == 0 else start + 2  # a killed publisher left it odd
    _write_hydrate_generation(data_dir, stable_id, opening)
    try:
        yield
    finally:
        try:
            _write_hydrate_generation(data_dir, stable_id, opening + 1)
        except HydrationError as exc:
            # Never raised over the publish itself. Left odd, the stripe reads
            # as a killed publisher's: readers take the free locks and read
            # under them, and the next publish on the stripe moves it on.
            _log.warning("stem-hydration: publish generation left odd for %s: %s", stable_id, exc)


def hydrate_owners_dir(data_dir: Path) -> Path:
    """Where each running hydrate publishes itself, per track, in every process."""
    return Path(data_dir) / "state" / "stem-hydrate-owners"


def _is_finite_stamp(value: object) -> bool:
    """True for finite numbers representable as float; false for bools and non-numerics."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        as_float = float(value)
    except OverflowError:
        return False
    return math.isfinite(as_float)


@contextlib.contextmanager
def _hydrate_owner_marked(data_dir: Path, stable_id: str) -> Iterator[str]:
    """Publish this process as a hydrator of ``stable_id`` for the duration.

    Written BEFORE the hydrate waits for any lock, so a hydrate queued behind
    another track's lock stripe is already visible as this track's fetch. The
    marker names its owner by pid and process start time, so a marker left by
    a killed process (or a pid since reused) reads as dead, never as a fetch.
    ``@`` cannot occur in a stable id, so ``<id>@`` prefixes only this track.

    Yields ``f\"{pid}-{token_hex(4)}\"`` so the hydrate temp directory can use
    the same attempt suffix and progress reads bind to this attempt only.
    """
    me = psutil.Process()
    owners = hydrate_owners_dir(data_dir)
    owners.mkdir(parents=True, exist_ok=True)
    attempt_suffix = f"{me.pid}-{secrets.token_hex(4)}"
    marker = owners / f"{stable_id}@{attempt_suffix}.json"
    staged = marker.with_name(f"{marker.name}.tmp")
    record = {"pid": me.pid, "create_time": me.create_time(), "started_at": time.time()}
    staged.write_text(json.dumps(record), encoding="utf-8", newline="\n")
    # A tombstone already beside this name would make the new owner read as
    # finished, so it is cleared first; one that cannot be cleared stops the publish.
    done = _done_sibling(marker)
    if done.exists():
        try:
            _retry_sharing_violation(lambda: done.unlink(missing_ok=True))
        except OSError as exc:
            _unlink_or_log(staged)
            raise HydrationError(f"could not clear {done}: {exc}") from exc
    _replace_or_raise(staged, marker)  # readers never see a half-written marker
    try:
        yield attempt_suffix
    finally:
        _finish_owner_marker(marker)


def _owner_attempt_suffix(marker: Path, stable_id: str) -> str:
    """The ``pid-token`` part after ``stable_id@`` and before ``.json``."""
    return marker.name.removeprefix(f"{stable_id}@").removesuffix(".json")


def _scan_hydrate_owners(data_dir: Path, stable_id: str) -> tuple[bool, frozenset[str]]:
    """Scan owner markers: any ``alive``, and suffixes for markers not provably ``dead``.

    Malformed markers and those in state ``dead`` are unlinked. Markers in
    ``unknown`` or ``expired`` are kept but excluded from ``not_dead_suffixes``.
    """
    validate_stable_id(stable_id)
    owners = hydrate_owners_dir(data_dir)
    if not owners.is_dir():
        return False, frozenset()
    alive = False
    not_dead_suffixes: set[str] = set()
    for marker in owners.glob(f"{glob.escape(stable_id)}@*.json"):
        if _owner_marker_done(marker):
            continue
        try:
            record = json.loads(_read_text_retrying(marker))
        except FileNotFoundError:
            continue  # its owner finished between the listing and this read
        except OSError as exc:
            # Unreadable even after retries: its owner cannot be named, so it
            # is not taken as a fetch (a running one would hide the error).
            _log.warning("stem-hydration: ignoring unreadable owner marker %s: %s", marker, exc)
            continue
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            # Markers are replaced in whole, so this is not a crash mid-write.
            _log.warning("stem-hydration: removing unreadable owner marker %s: %s", marker, exc)
            _unlink_or_log(marker)
            continue
        if not _owner_record_has_shape(record):
            # Valid JSON that is not an owner record cannot name a pid to check.
            _log.warning("stem-hydration: removing malformed owner marker %s", marker)
            _unlink_or_log(marker)
            continue
        owner = _hydrate_owner_state(record)
        suffix = _owner_attempt_suffix(marker, stable_id)
        if owner == "alive":
            alive = True
            not_dead_suffixes.add(suffix)
        elif owner == "dead":
            _unlink_or_log(marker)
        elif owner in ("unknown", "expired"):
            not_dead_suffixes.add(suffix)
        else:
            raise AssertionError(f"unhandled owner state {owner!r}")
    return alive, frozenset(not_dead_suffixes)


def hydrate_owner_snapshot(data_dir: Path, stable_id: str) -> tuple[bool, frozenset[str]]:
    """Whether any owner marker is ``alive``, and suffixes for markers not provably ``dead``."""
    return _scan_hydrate_owners(data_dir, stable_id)


def hydrate_owner_alive(data_dir: Path, stable_id: str) -> bool:
    """Whether a running process has published itself as hydrating ``stable_id``.

    A marker whose owner is gone (no such pid, or the pid now belongs to a
    process started at another time) is removed and ignored. An unsafe
    ``stable_id`` raises :class:`StemArtifactError` before any path is built.
    """
    return hydrate_owner_snapshot(data_dir, stable_id)[0]


def hydrate_owner_attempt_suffixes(data_dir: Path, stable_id: str) -> frozenset[str]:
    """Owner-marker suffixes (``pid-token``) for markers not provably dead."""
    return hydrate_owner_snapshot(data_dir, stable_id)[1]


#: Longest a hydrate (fetch, publish, verify) is taken to run. An owner marker
#: older than this is not counted as a fetch in any process, which bounds a
#: marker its live owner could neither remove nor mark done.
HYDRATE_OWNER_TTL_S: float = 900.0

#: How far a live owner's start time may read from the one it recorded. psutil
#: derives it from the boot time on Linux, which can shift slightly when the
#: clock is adjusted; a reused pid starts far later than one second after.
_OWNER_START_TOLERANCE_S: float = 1.0


def _owner_record_has_shape(record: object) -> bool:
    """True when a decoded marker carries ``pid`` and finite stamps ``create_time``
    and (when present and not JSON null) ``started_at`` -- the fields
    :func:`_hydrate_owner_state` reads."""
    if not isinstance(record, dict):
        return False
    pid = record.get("pid")
    create_time = record.get("create_time")
    started_raw = record.get("started_at")
    if not (isinstance(pid, int) and not isinstance(pid, bool) and pid > 0 and _is_finite_stamp(create_time)):
        return False
    if started_raw is None:
        return True
    return _is_finite_stamp(started_raw)


def _hydrate_owner_state(
    record: dict[str, object],
) -> Literal["alive", "dead", "unknown", "expired"]:
    """``alive``: the recorded pid runs and started when recorded. ``dead``: it
    is gone or the pid was reused (the marker may be removed). ``unknown``:
    its start time cannot be read. ``expired``: the marker is older than
    :data:`HYDRATE_OWNER_TTL_S` (or predates that stamp) and its owner is still
    running or cannot be inspected. Neither of the last two is counted as a
    fetch, and both markers are kept, since their owner may still be running.
    The pid is checked before the age, so a dead owner's marker is ``dead``
    (and removed) whatever its age."""
    started_at = record.get("started_at")
    expired = not isinstance(started_at, (int, float)) or time.time() - started_at > HYDRATE_OWNER_TTL_S
    try:
        process = psutil.Process(int(record["pid"]))  # type: ignore[call-overload]
        started = float(record["create_time"])  # type: ignore[arg-type]
        reused = abs(process.create_time() - started) >= _OWNER_START_TOLERANCE_S
    except psutil.NoSuchProcess:  # also ZombieProcess: exited, not yet reaped
        return "dead"
    except (psutil.AccessDenied, OSError) as exc:
        _log.warning("stem-hydration: cannot inspect owner pid %s: %s", record.get("pid"), exc)
        return "expired" if expired else "unknown"
    if reused:
        return "dead"
    return "expired" if expired else "alive"
