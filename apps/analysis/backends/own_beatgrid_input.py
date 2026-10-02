"""The file Beat This! reads for one track: the track, or the engine's decode of it (NAE-22).

Split from :mod:`apps.analysis.backends.own_beatgrid`, which calls
:func:`runner_input` around every runner invocation.
"""

from __future__ import annotations

import hashlib
import os
import time
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


def _take_lock(lock: Path, audio_path: Path, stale_after_s: float) -> None:
    for _ in range(2):
        try:
            os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except FileNotFoundError:
                continue
            if age < stale_after_s:
                break
            lock.unlink(missing_ok=True)
        else:
            return
    # Another worker is analyzing this track now. Not a fault of the file:
    # retried later, as for a file that was not there to analyze.
    raise TrackVanished(f"{audio_path} is being analyzed by another run")


@contextmanager
def runner_input(
    audio_path: Path, *, stale_after_s: float, decode_dir: Path | None = None
) -> Iterator[Path]:
    """The file the runner reads: the track itself, or the engine's decode of it.

    The runner reads audio through torchaudio, soundfile and madmom, none of
    which opens an m4a without ffmpeg, and the shipped app has no ffmpeg
    (NAE-22). For a container libsndfile cannot read, the engine decodes it
    into a float WAV first, at a path fixed per track so the activation file
    is the same on every run of it. A lock beside it keeps two runs of one
    track from writing the WAV under each other; both go on exit. A lock
    older than ``stale_after_s`` is a crashed run's and is taken over.
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
    _take_lock(lock, audio_path, stale_after_s)
    try:
        try:
            engine_decode.decode_to_wav(audio_path, wav)
        except engine_decode.EngineDecoderUnavailable as exc:
            raise BackendNotAvailable(f"the engine cannot decode {audio_path}: {exc}") from exc
        except engine_decode.EngineDecodeFailed as exc:
            if not audio_path.exists():
                raise TrackVanished(f"{audio_path} vanished before the engine decoded it") from None
            raise TrackUnreadable(str(exc)) from None
        except OSError as exc:
            # Writing the WAV failed (a full disk, a permission): the host's
            # fault, and it fails the next track the same way.
            raise BackendNotAvailable(f"could not write the engine decode {wav}: {exc}") from exc
        yield wav
    finally:
        wav.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)
