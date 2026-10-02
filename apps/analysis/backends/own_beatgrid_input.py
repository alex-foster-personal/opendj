"""The file Beat This! reads for one track: the track, or the engine's decode of it (NAE-22).

Split from :mod:`apps.analysis.backends.own_beatgrid`, which calls
:func:`runner_input` around every runner invocation.
"""

from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from apps.analysis_beatgrid import activations
from apps.shared import engine_decode

from .base import BackendNotAvailable, TrackUnreadable, TrackVanished


def _engine_decode_dir() -> Path:
    """Where an m4a's engine decode waits for the runner.

    Fixed, not a fresh temporary directory, because the runner names its
    activation file after a digest of the path it read; under the data dir,
    beside the activations, rather than a shared temp dir.
    """
    return activations.default_activations_dir().parent / "engine-decode"


# The platform seam for the per-track lock: an OS lock on an open file, which
# the OS drops when its holder exits or dies, so a crashed run never leaves a
# lock behind to be taken over (and no takeover can race a live holder).
if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True


@contextmanager
def _track_lock(lock: Path, audio_path: Path) -> Iterator[None]:
    """Held for one run of one track; released on exit, or by the OS on a crash."""
    fd = os.open(lock, os.O_CREAT | os.O_RDWR)
    try:
        if not _try_lock(fd):
            # Another worker is analyzing this track now. Not a fault of the
            # file: retried later, as for a file that was not there to analyze.
            raise TrackVanished(f"{audio_path} is being analyzed by another run")
        yield
    finally:
        # Closing drops the lock. The lock file stays: unlinking it would let
        # a waiter lock the old inode while a newcomer locks a new one.
        os.close(fd)


@contextmanager
def runner_input(audio_path: Path, *, decode_dir: Path | None = None) -> Iterator[Path]:
    """The file the runner reads: the track itself, or the engine's decode of it.

    The runner reads audio through torchaudio, soundfile and madmom, none of
    which opens an m4a without ffmpeg, and the shipped app has no ffmpeg
    (NAE-22). For a container libsndfile cannot read, the engine decodes it
    into a float WAV first, at a path fixed per track so the activation file
    is the same on every run of it. A lock beside it keeps two runs of one
    track from writing the WAV under each other; the WAV goes on exit.
    """
    if not engine_decode.needs_engine_decode(audio_path):
        yield audio_path
        return
    tag = hashlib.sha256(str(audio_path.resolve()).encode("utf-8")).hexdigest()[:12]
    folder = (decode_dir or _engine_decode_dir()) / tag
    wav = folder / f"{audio_path.stem}.wav"
    lock = folder / ".lock"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackendNotAvailable(f"cannot create {folder} for the engine decode: {exc}") from exc
    with _track_lock(lock, audio_path):
        try:
            try:
                engine_decode.decode_to_wav(audio_path, wav)
            except engine_decode.EngineDecoderUnavailable as exc:
                raise BackendNotAvailable(
                    f"the engine cannot decode {audio_path}: {exc}"
                ) from exc
            except engine_decode.EngineDecodeFailed as exc:
                if not audio_path.exists():
                    raise TrackVanished(
                        f"{audio_path} vanished before the engine decoded it"
                    ) from None
                raise TrackUnreadable(str(exc)) from None
            except OSError as exc:
                # Writing the WAV failed (a full disk, a permission): the
                # host's fault, and it fails the next track the same way.
                raise BackendNotAvailable(
                    f"could not write the engine decode {wav}: {exc}"
                ) from exc
            yield wav
        finally:
            wav.unlink(missing_ok=True)
