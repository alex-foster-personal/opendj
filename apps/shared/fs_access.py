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
from pathlib import Path

from apps.shared.platform_paths import HOME, MUSIC_ROOTS

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
    can fix from System Settings.
    """

    path: str
    exists: bool
    readable: bool
    denied: bool
    detail: str

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
        )
    if not path.is_dir():
        return AccessProbe(
            path=str(path),
            exists=True,
            readable=False,
            denied=False,
            detail="exists but is not a directory",
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
        )
    except OSError as exc:
        return AccessProbe(
            path=str(path),
            exists=True,
            readable=False,
            denied=False,
            detail=f"could not be listed: {exc.strerror or exc}",
        )
    return AccessProbe(
        path=str(path),
        exists=True,
        readable=True,
        denied=False,
        detail="readable",
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


def probe_all(paths: list[Path]) -> list[AccessProbe]:
    """Probe each path once, in order, with duplicates dropped."""
    seen: set[Path] = set()
    probes: list[AccessProbe] = []
    for candidate in paths:
        resolved = candidate.expanduser()
        if resolved in seen:
            continue
        seen.add(resolved)
        probes.append(probe_readable(resolved))
    return probes


def denied_roots(probes: list[AccessProbe]) -> list[str]:
    """Just the paths macOS refused. The honest-denominator caveat list."""
    return [probe.path for probe in probes if probe.denied]


__all__ = [
    "CANDIDATE_MUSIC_FOLDERS",
    "GRANT_INSTRUCTIONS",
    "AccessProbe",
    "denied_roots",
    "music_folder_candidates",
    "music_root_access",
    "probe_all",
    "probe_readable",
]
