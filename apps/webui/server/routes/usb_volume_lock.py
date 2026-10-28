"""USB writes and ejects never overlap, across every client of this daemon.

An export write and an eject of the same stick must never overlap: an eject
that lands while apply copies, fsyncs or promotes the stick's export database leaves a
partial export and an outcome nobody can state (Codex P1 4300896677 on #6826).
The page's own busy flag covers one tab only, so the hold lives here.

A write holds its volume's resolved root, so writes to different sticks still
run side by side. An eject holds every volume: ``diskutil eject`` unmounts every
volume on the whole disk, so ejecting one partition of a stick would unmount a
sibling partition another request is writing (Codex P1 4301299382). Nothing
waits: a busy volume is refused at once, and the caller says so.

An eject whose diskutil did not answer in time may still unmount the disk
later, after the request has returned. So it keeps every volume held for a
settle window past its own end, and a write in that window is refused rather
than started on a disk that may vanish under it (Codex P1 4352472811).
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_GUARD = threading.Lock()
_WRITING: set[str] = set()
#: The root of the eject in flight, when one is; it holds every volume.
_EJECTING: set[str] = set()
#: Ejects that timed out: root -> the ``time.monotonic()`` their hold ends.
_UNSETTLED: dict[str, float] = {}


class VolumeBusy(Exception):
    """Another USB operation holds this volume."""

    def __init__(self, root: str) -> None:
        super().__init__(root)
        self.root = root


def _key(root: str | Path) -> str:
    return os.path.realpath(os.fspath(root))


def _busy_locked() -> bool:
    """True while an eject runs or a timed-out one is still settling; caller holds ``_GUARD``."""
    now = time.monotonic()
    for key, until in list(_UNSETTLED.items()):
        if now >= until:
            del _UNSETTLED[key]
    return bool(_EJECTING or _UNSETTLED)


def hold_unsettled_eject(root: str | Path, seconds: float) -> None:
    """Keep every volume held for ``seconds`` after an eject whose outcome is not known yet.

    Called from inside :func:`eject_operation` when diskutil timed out: the
    unmount it asked for may still land, so the hold outlives the request.
    """
    with _GUARD:
        _UNSETTLED[_key(root)] = time.monotonic() + seconds


@contextmanager
def volume_operation(root: str | Path) -> Iterator[None]:
    """Hold ``root`` for one write, or raise :class:`VolumeBusy` without waiting."""
    key = _key(root)
    with _GUARD:
        if _busy_locked() or key in _WRITING:
            raise VolumeBusy(key)
        _WRITING.add(key)
    try:
        yield
    finally:
        with _GUARD:
            _WRITING.discard(key)


@contextmanager
def eject_operation(root: str | Path) -> Iterator[None]:
    """Hold every volume for one eject of ``root``'s disk, or raise :class:`VolumeBusy` without waiting.

    The probe cannot list a disk's sibling volumes without another diskutil call
    per mount, so the eject holds them all: it is refused while any write or
    other eject runs, and every write is refused while it runs.
    """
    key = _key(root)
    with _GUARD:
        if _busy_locked() or _WRITING:
            raise VolumeBusy(key)
        _EJECTING.add(key)
    try:
        yield
    finally:
        with _GUARD:
            _EJECTING.discard(key)
