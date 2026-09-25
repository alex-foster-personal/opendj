"""Bounded listing of a volume's root, typed by whether it may be read (USBPLAY-02).

macOS gates reading a removable volume behind the Removable Volumes privacy
permission. While its prompt is on screen, ``listdir`` of the volume root does
not return; once denied, it raises ``PermissionError`` (EPERM). Before this
module the scanner listed each root inline (``has_dj_export_at_root`` and
``classify_mount``), so an open prompt stalled ``GET /usb/volumes`` until it
was answered, and a denial read as "no DJ export" and could fold a USB SSD
away as a mounted drive.

Each root is now listed on a daemon worker thread and awaited against ONE
deadline shared by the whole scan, so a hung listing costs the request at
most :data:`ROOT_LISTING_TIMEOUT_S` however many volumes hang:

* ``ok``      the root listed; its names are returned.
* ``pending`` the listing had not answered by the deadline: the permission
              prompt is open, or the drive is still spinning up. The two are
              indistinguishable from here, and both clear on a later scan.
* ``denied``  ``PermissionError``: the permission was refused.
* ``unknown`` the listing failed for another reason (logged), or no listing
              ran at all (simulated volumes; the ``UsbVolume`` default).

A blocked ``listdir`` cannot be cancelled from Python. At most ONE listing per
mount path is in flight: a scan that finds a listing still running from an
earlier scan awaits that one instead of starting another, so an unanswered
prompt holds one parked thread per volume, never one per scan. The
subprocess probe in ``apps.shared.bounded_file_open`` is killable but reads a
file byte, not a directory, and costs an interpreter spawn per volume per
scan; a parked thread that finishes when the prompt is answered is the
cheaper bound here.

-Claude
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)

RootAccess = Literal["ok", "pending", "denied", "unknown"]

#: The whole-scan budget for root listings; GET /usb/volumes must answer
#: within about 1 s with a prompt open, and diskutil runs in parallel.
ROOT_LISTING_TIMEOUT_S: float = 0.75


@dataclass(frozen=True)
class RootListing:
    access: RootAccess
    #: The root's entry names; empty unless ``access == "ok"``.
    names: frozenset[str] = frozenset()


@dataclass
class RootListingProbe:
    mount: str
    done: threading.Event = field(default_factory=threading.Event)
    result: RootListing | None = None


_IN_FLIGHT: dict[str, RootListingProbe] = {}
_IN_FLIGHT_LOCK = threading.Lock()


# ----- public -------------------------------------------------------------------


def start_root_listing(mount: Path) -> RootListingProbe:
    """Start listing ``mount`` on a worker, or join the listing already running."""
    key = str(mount)
    with _IN_FLIGHT_LOCK:
        running = _IN_FLIGHT.get(key)
        if running is not None:
            return running
        probe = RootListingProbe(mount=key)
        _IN_FLIGHT[key] = probe
    threading.Thread(
        target=_run_listing,
        args=(probe,),
        name=f"usb-root-listing:{mount.name}",
        daemon=True,
    ).start()
    return probe


def await_root_listing(probe: RootListingProbe, *, deadline_mono: float) -> RootListing:
    """The listing's result, or ``pending`` if it has not answered by the deadline."""
    if probe.done.wait(timeout=max(0.0, deadline_mono - time.monotonic())):
        assert probe.result is not None  # set before done, in _run_listing
        return probe.result
    return RootListing(access="pending")


def root_listing_deadline() -> float:
    """One monotonic deadline for every listing of one scan."""
    return time.monotonic() + ROOT_LISTING_TIMEOUT_S


# ----- worker -------------------------------------------------------------------


def _run_listing(probe: RootListingProbe) -> None:
    try:
        probe.result = _list_root(probe.mount)
    finally:
        with _IN_FLIGHT_LOCK:
            if _IN_FLIGHT.get(probe.mount) is probe:
                del _IN_FLIGHT[probe.mount]
        probe.done.set()


def _list_root(mount: str) -> RootListing:
    try:
        names = os.listdir(mount)
    except PermissionError as exc:
        log.warning("usb volume %s: root listing refused: %s", mount, exc)
        return RootListing(access="denied")
    except OSError as exc:
        log.warning("usb volume %s: root listing failed: %s", mount, exc)
        return RootListing(access="unknown")
    return RootListing(access="ok", names=frozenset(names))


# ----- test helpers ---------------------------------------------------------------


def _in_flight_mounts_for_tests() -> list[str]:
    with _IN_FLIGHT_LOCK:
        return sorted(_IN_FLIGHT)


def _reset_for_tests() -> None:
    with _IN_FLIGHT_LOCK:
        _IN_FLIGHT.clear()


__all__ = [
    "ROOT_LISTING_TIMEOUT_S",
    "RootAccess",
    "RootListing",
    "RootListingProbe",
    "await_root_listing",
    "root_listing_deadline",
    "start_root_listing",
]
