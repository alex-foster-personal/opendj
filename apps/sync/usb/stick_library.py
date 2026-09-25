"""Play from USB: one resolver for every stick route (USBPLAY-03/04/06).

id -> VolumeUUID -> the mounted volume a FRESH discovery scan reports (never
a client-supplied path) -> the cached ``export.pdb`` parse -> the pdb row ->
a file path joined to the mount and proven to stay inside it. The HTTP layer
(``apps/webui/server/routes/usb_tracks.py``) injects the scan, because this
package may not import the web UI (``.importlinter``). Ids, errors and the
library model live in :mod:`apps.sync.usb.stick_model`, re-exported here.

Cache. A parse is kept per VolumeUUID and reused while ``export.pdb``'s
``(st_size, st_mtime_ns)`` is unchanged, which one ``stat`` proves per
request. A mount is bound to its UUID by a fresh scan and trusted while the
mount's ``st_dev`` is unchanged; any change to the pdb under a trusted
binding re-proves the binding with a fresh scan first, so a different stick
remounted at the same path (two sticks both named "NO NAME") is never served
under the first stick's ids.

Read only: nothing here opens a stick file for writing (USBPLAY-08).
"""
from __future__ import annotations

import errno as errno_codes
import os
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from apps.shared.bounded_file_open import AUDIO_ACCESS_TIMEOUT_S, probe_readable_byte
from apps.shared.state.locations import AUDIO_MEDIA_TYPES
from apps.sync.usb.pioneer.reader import read_export_pdb
from apps.sync.usb.stick_model import (
    HISTORY_ID_PREFIX,
    PLAYLIST_ID_PREFIX,
    TRACK_ID_PREFIX,
    StickError,
    StickErrorCode,
    StickHistory,
    StickLibrary,
    StickPlaylist,
    StickTrack,
    StickTrackRef,
    build_stick_library,
    is_canonical_volume_uuid,
    mint_stick_track_id,
    normalize_key,
    parse_stick_track_id,
)

PathRefusal = Literal[
    "invalid_path", "escapes_mount", "outside_allowed_dir", "extension_not_allowed"
]
ArtworkSize = Literal["s", "m", "orig"]
AnlzSuffix = Literal[".DAT", ".EXT", ".2EX"]

EXPORT_PDB_PARTS: tuple[str, ...] = ("PIONEER", "rekordbox", "export.pdb")
ARTWORK_DIR_PARTS: tuple[str, ...] = ("PIONEER", "Artwork")
ANLZ_DIR_PARTS: tuple[str, ...] = ("PIONEER", "USBANLZ")
_ARTWORK_SUFFIXES = frozenset({".jpg"})
_ANLZ_SUFFIXES = frozenset({".dat", ".ext", ".2ex"})
_BLOCKED_ERRNOS = frozenset({errno_codes.EACCES, errno_codes.EPERM})
_MISSING_ERRNOS = frozenset({errno_codes.ENOENT, errno_codes.ENOTDIR})


# ----- mounted sticks -------------------------------------------------------


@dataclass(frozen=True)
class MountedVolume:
    """One row of a discovery scan, as the HTTP layer hands it over."""

    volume_id: str
    volume_uuid: str | None
    name: str
    mount_path: Path


@dataclass(frozen=True)
class MountedStick:
    volume_id: str
    volume_uuid: str
    name: str
    mount: Path


#: A FRESH scan of mounted volumes (the injected discovery seam).
VolumeScan = Callable[[], Sequence[MountedVolume]]


@dataclass(frozen=True)
class OpenedStickLibrary:
    stick: MountedStick
    library: StickLibrary
    cache_hit: bool


@dataclass(frozen=True)
class ResolvedStickTrack:
    stick: MountedStick
    library: StickLibrary
    track: StickTrack


# ----- binding and cache ----------------------------------------------------


@dataclass(frozen=True)
class _Binding:
    stick: MountedStick
    st_dev: int


@dataclass(frozen=True)
class _CachedLibrary:
    fingerprint: tuple[int, int]
    library: StickLibrary


_LOCK = threading.Lock()
_bindings: dict[str, _Binding] = {}
_libraries: dict[str, _CachedLibrary] = {}


def open_stick_library(volume_uuid: str, scan: VolumeScan) -> OpenedStickLibrary:
    """The stick's parsed library, from cache while ``export.pdb`` is unchanged."""
    if not is_canonical_volume_uuid(volume_uuid):
        raise ValueError(f"volume uuid {volume_uuid!r} is not uppercase-hex 8-4-4-4-12")
    with _LOCK:
        stick = _trusted_binding(volume_uuid)
        freshly_bound = stick is None
        if stick is None:
            stick = _bind_from_fresh_scan(volume_uuid, scan)
        fingerprint = _pdb_fingerprint(stick)
        cached = _libraries.get(volume_uuid)
        if cached is not None and fingerprint == cached.fingerprint:
            return OpenedStickLibrary(stick=stick, library=cached.library, cache_hit=True)
        if not freshly_bound:
            # The pdb changed or vanished under a trusted binding: prove which
            # stick is mounted there before parsing anything under this UUID.
            stick = _bind_from_fresh_scan(volume_uuid, scan)
            fingerprint = _pdb_fingerprint(stick)
            if cached is not None and fingerprint == cached.fingerprint:
                return OpenedStickLibrary(stick=stick, library=cached.library, cache_hit=True)
        if fingerprint is None:
            raise StickError(
                "USB_FILE_MISSING",
                f"no rekordbox export at {'/'.join(EXPORT_PDB_PARTS)} on this stick",
                volume_uuid=volume_uuid,
            )
        library = _parse_library(stick, fingerprint)
        _libraries[volume_uuid] = _CachedLibrary(fingerprint=fingerprint, library=library)
        return OpenedStickLibrary(stick=stick, library=library, cache_hit=False)


def resolve_stick_track(track_id: str, scan: VolumeScan) -> ResolvedStickTrack:
    ref = parse_stick_track_id(track_id)
    opened = open_stick_library(ref.volume_uuid, scan)
    track = opened.library.tracks_by_pdb_id.get(ref.pdb_id)
    if track is None:
        raise StickError(
            "USB_TRACK_NOT_FOUND",
            f"the stick's export has no track {ref.pdb_id}",
            volume_uuid=ref.volume_uuid,
        )
    return ResolvedStickTrack(stick=opened.stick, library=opened.library, track=track)


def _trusted_binding(volume_uuid: str) -> MountedStick | None:
    binding = _bindings.get(volume_uuid)
    if binding is None:
        return None
    if _mount_device(binding.stick.mount) != binding.st_dev:
        del _bindings[volume_uuid]
        return None
    return binding.stick


def _mount_device(mount: Path) -> int | None:
    """The mount's device id, or None when nothing is mounted there. A
    module-level seam: tests substitute this host fact as they substitute
    discovery's."""
    try:
        return os.stat(mount).st_dev
    except (FileNotFoundError, NotADirectoryError):
        return None


def _bind_from_fresh_scan(volume_uuid: str, scan: VolumeScan) -> MountedStick:
    matches = [volume for volume in scan() if volume.volume_uuid == volume_uuid]
    if not matches:
        _bindings.pop(volume_uuid, None)
        raise StickError(
            "USB_STICK_NOT_MOUNTED",
            "no mounted volume has this VolumeUUID",
            volume_uuid=volume_uuid,
        )
    if len(matches) > 1:
        raise RuntimeError(
            f"{len(matches)} mounted volumes share VolumeUUID {volume_uuid} "
            f"({', '.join(str(m.mount_path) for m in matches)}); stick ids would be ambiguous"
        )
    volume = matches[0]
    stick = MountedStick(
        volume_id=volume.volume_id,
        volume_uuid=volume_uuid,
        name=volume.name,
        mount=volume.mount_path,
    )
    _probe_export_readable(stick)
    st_dev = _mount_device(stick.mount)
    if st_dev is None:
        raise StickError(
            "USB_STICK_NOT_MOUNTED",
            f"{stick.mount} went away while it was being bound",
            volume_uuid=volume_uuid,
        )
    _bindings[volume_uuid] = _Binding(stick=stick, st_dev=st_dev)
    return stick


def _probe_export_readable(stick: MountedStick) -> None:
    """First contact with a stick goes through a killable subprocess: a
    pending macOS Removable Volumes prompt blocks ``open()`` until answered,
    and that must not hang a request thread (USBPLAY-02)."""
    pdb_path = stick.mount.joinpath(*EXPORT_PDB_PARTS)
    probe = probe_readable_byte(pdb_path, timeout_s=AUDIO_ACCESS_TIMEOUT_S)
    if probe.outcome == "ok":
        return
    if probe.outcome == "timeout" or probe.errno in _BLOCKED_ERRNOS:
        raise StickError(
            "USB_STICK_ACCESS_BLOCKED",
            f"reading {pdb_path} was refused or did not return within "
            f"{AUDIO_ACCESS_TIMEOUT_S:.0f}s ({probe.outcome}: {probe.message})",
            volume_uuid=stick.volume_uuid,
        )
    if probe.errno in _MISSING_ERRNOS:
        raise StickError(
            "USB_FILE_MISSING",
            f"no rekordbox export at {'/'.join(EXPORT_PDB_PARTS)} on this stick",
            volume_uuid=stick.volume_uuid,
        )
    raise OSError(probe.errno or 0, f"probing {pdb_path} failed: {probe.message}")


def _pdb_fingerprint(stick: MountedStick) -> tuple[int, int] | None:
    try:
        st = os.stat(stick.mount.joinpath(*EXPORT_PDB_PARTS))
    except (FileNotFoundError, NotADirectoryError):
        return None
    return (st.st_size, st.st_mtime_ns)


def _parse_library(stick: MountedStick, fingerprint: tuple[int, int]) -> StickLibrary:
    pdb_path = stick.mount.joinpath(*EXPORT_PDB_PARTS)
    if not pdb_path.resolve().is_relative_to(stick.mount.resolve()):
        raise StickError(
            "USB_PATH_OUTSIDE_VOLUME",
            f"{pdb_path} resolves outside the stick",
            reason="escapes_mount",
        )
    started = time.perf_counter()
    try:
        # The PIONEER dir, not the mount: a stick NAMED "PIONEER" would
        # otherwise be read as its own PIONEER dir (_resolve_pioneer_root).
        raw = read_export_pdb(stick.mount.joinpath(EXPORT_PDB_PARTS[0]))
    except FileNotFoundError as exc:
        raise StickError(
            "USB_FILE_MISSING",
            f"no rekordbox export at {'/'.join(EXPORT_PDB_PARTS)} on this stick",
            volume_uuid=stick.volume_uuid,
        ) from exc
    parse_ms = (time.perf_counter() - started) * 1000.0
    return build_stick_library(stick.volume_uuid, raw, fingerprint, parse_ms)


# ----- files on the stick ---------------------------------------------------


@dataclass(frozen=True)
class StickAudioFile:
    path: Path
    media_type: str


def stick_audio_file(resolved: ResolvedStickTrack) -> StickAudioFile:
    """The track's audio: contained, audio-extension allowlisted, present."""
    path = _existing_stick_file(
        resolved,
        resolved.track.file_path,
        allowed_dir=(),
        allowed_suffixes=frozenset(AUDIO_MEDIA_TYPES),
        what="audio file",
    )
    return StickAudioFile(path=path, media_type=AUDIO_MEDIA_TYPES[path.suffix.lower()])


def stick_audio_path(resolved: ResolvedStickTrack) -> Path:
    """Where the audio must be, policy-checked, WITHOUT a stat (metadata use)."""
    return _contained_stick_path(
        resolved.stick,
        _required(resolved, resolved.track.file_path, "audio file"),
        allowed_dir=(),
        allowed_suffixes=frozenset(AUDIO_MEDIA_TYPES),
    )


def stick_artwork_file(resolved: ResolvedStickTrack, size: ArtworkSize) -> Path:
    """``s`` is the pdb's jpg (80x80); ``m`` and ``orig`` are its ``_m``
    sibling (240x240), the largest rendering rekordbox writes to a stick."""
    relative = _required(resolved, resolved.track.artwork_path, "artwork")
    if size == "s":
        target = relative
    elif size in ("m", "orig"):
        pure = PurePosixPath(relative)
        target = str(pure.with_name(f"{pure.stem}_m{pure.suffix}"))
    else:
        raise ValueError(f"artwork size must be s, m or orig, got {size!r}")
    return _existing_stick_file(
        resolved,
        target,
        allowed_dir=ARTWORK_DIR_PARTS,
        allowed_suffixes=_ARTWORK_SUFFIXES,
        what="artwork",
    )


def stick_anlz_file(resolved: ResolvedStickTrack, suffix: AnlzSuffix) -> Path:
    """One of the track's own ANLZ files, from its exact ``analyze_path``
    (never by scanning the directory: ``ANLZ0000.*`` and ``ANLZ0001.*`` can
    share one, USBPLAY-07)."""
    relative = _required(resolved, resolved.track.analyze_path, "analysis")
    target = str(PurePosixPath(relative).with_suffix(suffix))
    return _existing_stick_file(
        resolved,
        target,
        allowed_dir=ANLZ_DIR_PARTS,
        allowed_suffixes=_ANLZ_SUFFIXES,
        what="analysis file",
    )


def _required(resolved: ResolvedStickTrack, relative: str | None, what: str) -> str:
    if not relative:
        raise StickError(
            "USB_FILE_MISSING",
            f"the export names no {what} for track {resolved.track.pdb_id}",
            volume_uuid=resolved.stick.volume_uuid,
        )
    return relative


def _existing_stick_file(
    resolved: ResolvedStickTrack,
    relative: str,
    *,
    allowed_dir: tuple[str, ...],
    allowed_suffixes: frozenset[str],
    what: str,
) -> Path:
    path = _contained_stick_path(
        resolved.stick,
        _required(resolved, relative, what),
        allowed_dir=allowed_dir,
        allowed_suffixes=allowed_suffixes,
    )
    if not path.is_file():
        raise StickError(
            "USB_FILE_MISSING",
            f"{what} {relative} is not on the stick",
            volume_uuid=resolved.stick.volume_uuid,
        )
    return path


def _contained_stick_path(
    stick: MountedStick,
    relative: str,
    *,
    allowed_dir: tuple[str, ...],
    allowed_suffixes: frozenset[str],
) -> Path:
    """Join a pdb path to the mount and prove it stays there.

    pdb paths are stick-relative with a leading ``/``; one is never treated
    as a host-absolute path. ``resolve()`` follows symlinks, so a link on
    the stick pointing elsewhere is caught by the same check as ``..``.
    """
    if "\x00" in relative:
        raise _path_refusal(stick, relative, "invalid_path")
    mount = stick.mount.resolve()
    candidate = stick.mount.joinpath(relative.lstrip("/")).resolve()
    if not candidate.is_relative_to(mount):
        raise _path_refusal(stick, relative, "escapes_mount")
    if not candidate.is_relative_to(mount.joinpath(*allowed_dir)):
        raise _path_refusal(stick, relative, "outside_allowed_dir")
    if candidate.suffix.lower() not in allowed_suffixes:
        raise _path_refusal(stick, relative, "extension_not_allowed")
    return candidate


def _path_refusal(stick: MountedStick, relative: str, reason: PathRefusal) -> StickError:
    return StickError(
        "USB_PATH_OUTSIDE_VOLUME",
        f"{relative!r} is not a file this route may serve from the stick ({reason})",
        volume_uuid=stick.volume_uuid,
        reason=reason,
    )


def _reset_for_tests() -> None:
    with _LOCK:
        _bindings.clear()
        _libraries.clear()


__all__ = [
    "ANLZ_DIR_PARTS",
    "ARTWORK_DIR_PARTS",
    "EXPORT_PDB_PARTS",
    "HISTORY_ID_PREFIX",
    "PLAYLIST_ID_PREFIX",
    "TRACK_ID_PREFIX",
    "AnlzSuffix",
    "ArtworkSize",
    "MountedStick",
    "MountedVolume",
    "OpenedStickLibrary",
    "PathRefusal",
    "ResolvedStickTrack",
    "StickAudioFile",
    "StickError",
    "StickErrorCode",
    "StickHistory",
    "StickLibrary",
    "StickPlaylist",
    "StickTrack",
    "StickTrackRef",
    "VolumeScan",
    "is_canonical_volume_uuid",
    "mint_stick_track_id",
    "normalize_key",
    "open_stick_library",
    "parse_stick_track_id",
    "resolve_stick_track",
    "stick_anlz_file",
    "stick_artwork_file",
    "stick_audio_file",
    "stick_audio_path",
]
