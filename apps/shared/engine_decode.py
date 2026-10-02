"""Decode audio with the app's own Rust engine (``odj-audio``), not ffmpeg.

Mini-PRD
--------
R1 ok   Resolve ``odj-audio`` as the engine supervisor does: ``ODJ_AUDIO_BIN``
        (set by every packaged payload's launcher) wins and is never
        second-guessed; without it, the newest local cargo build in this
        checkout.
R2 ok   :func:`probe_duration_s` reports the playable length the container
        states (``odj-audio probe``), or a full decode's frame count when it
        states none; ``None`` when the engine cannot read the file. Never an
        estimate from bitrate.
R3 ok   Fail loudly on the host: an absent or unusable binary raises
        :class:`EngineDecoderUnavailable`; it never reads as a fact about a file.

Acceptance
----------
[if] ODJ_AUDIO_BIN names an executable file  [then] it is used, no repo build read
[if] ODJ_AUDIO_BIN is set but not executable [then] EngineDecoderUnavailable names it
[if] ODJ_AUDIO_BIN is unset                  [then] the newest repo release/debug build
[if] no binary at all                        [then] EngineDecoderUnavailable
[if] a binary predates decode/probe          [then] it is passed over (repo) or refused (env)
[if] the file is not audio                   [then] probe_duration_s returns None

Why: the shipped app bundles ``odj-audio`` (``scripts/build_engine_payload.py``
stages ``bin/odj-audio`` and exports ``ODJ_AUDIO_BIN``) but no ffmpeg, so the
analysis lanes that shell out to ffmpeg cannot run on an installed app unless
the user has installed ffmpeg themselves. The engine decodes with symphonia
(MPL-2.0), in-process, with the MP4 edit list applied; measured against
ffmpeg 6.1 on Thu 1 Oct 2026 it matched sample-for-sample in time on WAV,
AIFF, FLAC, ALAC, MP3, Vorbis and AAC m4a. See
``research/audio-decode/2026-10-01-packaging-audio-decode.md``.

The resolver mirrors ``apps.engine_core.audio_engine.resolve_binary`` rather
than importing it, because ``apps.shared`` is the stable core and may not
import ``apps.engine_core`` (``.importlinter``).
"""

from __future__ import annotations

import functools
import json
import os
import struct
import subprocess
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path

from apps.shared.paths import PROJECT_ROOT

BIN_ENV = "ODJ_AUDIO_BIN"
"""Set by the packaged payload's launcher to its bundled ``bin/odj-audio``."""

EXE_NAME = "odj-audio.exe" if os.name == "nt" else "odj-audio"
REPO_TARGET = Path("apps/audio-engine/target")
PROBE_TIMEOUT_S = 120
"""A header read is instant; the bound covers a full-decode count of a long
MP3 with no Xing header (about 0.6 s per 6 minutes, measured in the 20-05
null test), on a contended host."""


#: Subcommands this module calls. A build older than them prints its usage
#: and exits 2 for each, which would read as "every file is unreadable".
REQUIRED_COMMANDS: tuple[str, ...] = ("decode", "probe")


#: Containers libsndfile (soundfile, and so librosa and Beat This!'s
#: fallback) reads by itself. Anything else, m4a/AAC/ALAC first, reached those
#: readers only through ffmpeg, which the shipped app does not bundle.
SNDFILE_SUFFIXES: frozenset[str] = frozenset(
    {".wav", ".wave", ".aif", ".aiff", ".aifc", ".flac", ".ogg", ".oga", ".mp3"}
)
DECODE_TIMEOUT_S = 300
_WAVE_FORMAT_IEEE_FLOAT = 3
_CHUNK_BYTES = 1 << 20
#: A RIFF size field is 32 bits and the header takes 36 of them.
_WAV_MAX_DATA_BYTES = 0xFFFFFFFF - 36


class EngineDecoderUnavailable(RuntimeError):
    """``odj-audio`` cannot be located or executed. Never a degraded result."""


class EngineDecodeFailed(RuntimeError):
    """``odj-audio`` ran and could not decode this one file."""


def needs_engine_decode(path: Path) -> bool:
    """Whether ``path`` is a container libsndfile cannot read by itself."""
    return path.suffix.lower() not in SNDFILE_SUFFIXES


@functools.lru_cache(maxsize=16)
def _has_commands(path: str, size: int, mtime_ns: int) -> bool:
    """Whether the binary's usage lists every required subcommand.

    Asked of the binary itself, because a reused CI workspace or an old local
    cargo build can hold an ``odj-audio`` that predates them (seen on the
    nucbox runner, Thu 1 Oct 2026). Cached by path, size and mtime, so a
    rebuilt binary is asked again.
    """
    del size, mtime_ns  # cache key only
    try:
        done = subprocess.run(  # fixed argv, never a shell
            [path, "help"], capture_output=True, text=True, errors="replace",
            check=False, stdin=subprocess.DEVNULL, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0 and all(
        f"odj-audio {cmd} " in done.stdout for cmd in REQUIRED_COMMANDS
    )


def _supports(p: Path) -> bool:
    st = p.stat()
    return _has_commands(str(p), st.st_size, st.st_mtime_ns)


def resolve_engine_decoder(
    environ: Mapping[str, str] | None = None, repo_root: Path = PROJECT_ROOT
) -> Path:
    """Path to ``odj-audio``: ``ODJ_AUDIO_BIN``, else the newest repo build."""
    env = os.environ if environ is None else environ
    raw = env.get(BIN_ENV, "").strip()
    if raw:
        p = Path(raw)
        if not (p.is_file() and os.access(p, os.X_OK)):
            raise EngineDecoderUnavailable(f"{BIN_ENV}={raw!r} is not an executable file")
        if not _supports(p):
            raise EngineDecoderUnavailable(
                f"{BIN_ENV}={raw!r} predates the {'/'.join(REQUIRED_COMMANDS)} subcommands"
            )
        return p
    found = [
        p
        for p in (repo_root / REPO_TARGET / profile / EXE_NAME for profile in ("release", "debug"))
        if p.is_file() and os.access(p, os.X_OK) and _supports(p)
    ]
    if not found:
        raise EngineDecoderUnavailable(
            f"no {BIN_ENV} and no local odj-audio build with "
            f"{'/'.join(REQUIRED_COMMANDS)}; run "
            "`cargo build --release --manifest-path apps/audio-engine/Cargo.toml`"
        )
    return max(found, key=lambda p: p.stat().st_mtime)


def probe_duration_s(path: Path, exe: Path | None = None) -> float | None:
    """Seconds of playable audio in ``path``, or ``None`` if the engine cannot read it."""
    binary = exe or resolve_engine_decoder()
    try:
        done = subprocess.run(  # fixed argv, never a shell
            [str(binary), "probe", str(path)],
            capture_output=True, text=True, errors="replace", check=False,
            stdin=subprocess.DEVNULL, timeout=PROBE_TIMEOUT_S,
        )
    except OSError as exc:
        raise EngineDecoderUnavailable(f"{binary} could not be launched: {exc}") from None
    except subprocess.TimeoutExpired:
        # The length is unknown THIS time; admission refuses it by name and a
        # later pass asks again.
        return None
    if done.returncode != 0:
        return None
    try:
        seconds = float(json.loads(done.stdout)["duration_s"])
    except (ValueError, KeyError, TypeError):
        return None
    return seconds if seconds > 0 else None


def _decode_argv(binary: Path, path: Path, *, mono: bool) -> list[str]:
    return [str(binary), "decode", *(["--mono"] if mono else []), "--format", "f32le", str(path)]


def _summary(stderr: bytes) -> dict[str, int]:
    """The JSON line ``odj-audio decode`` ends its stderr with."""
    lines = stderr.decode("utf-8", "replace").strip().splitlines()
    try:
        summary = json.loads(lines[-1])
        return {k: int(summary[k]) for k in ("sample_rate", "channels", "frames")}
    except (IndexError, ValueError, KeyError, TypeError):
        raise EngineDecodeFailed(
            f"odj-audio decode gave no summary: {lines[-1] if lines else 'no stderr'}"
        ) from None


def decode_f32(
    path: Path, *, mono: bool, exe: Path | None = None
) -> tuple[bytes, int, int]:
    """``(little-endian float32 PCM, sample_rate, channels)`` at the file's own rate.

    Raises :class:`EngineDecodeFailed` for a file the engine cannot read and
    :class:`EngineDecoderUnavailable` when the engine itself cannot run.
    """
    binary = exe or resolve_engine_decoder()
    try:
        done = subprocess.run(  # fixed argv, never a shell
            _decode_argv(binary, path, mono=mono),
            capture_output=True, check=False, stdin=subprocess.DEVNULL,
            timeout=DECODE_TIMEOUT_S,
        )
    except OSError as exc:
        raise EngineDecoderUnavailable(f"{binary} could not be launched: {exc}") from None
    except subprocess.TimeoutExpired:
        raise EngineDecodeFailed(
            f"odj-audio did not decode {path} within {DECODE_TIMEOUT_S}s"
        ) from None
    if done.returncode != 0:
        tail = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise EngineDecodeFailed(
            f"odj-audio could not decode {path}: {tail[-1] if tail else done.returncode}"
        )
    summary = _summary(done.stderr)
    if not done.stdout or len(done.stdout) != 4 * summary["channels"] * summary["frames"]:
        raise EngineDecodeFailed(
            f"odj-audio decoded {len(done.stdout)} bytes of {path}, not the "
            f"{summary['frames']} frames its summary states"
        )
    return done.stdout, summary["sample_rate"], summary["channels"]


def _wav_header(data_bytes: int, sample_rate: int, channels: int) -> bytes:
    """A WAVE_FORMAT_IEEE_FLOAT header for 32-bit float PCM."""
    block = 4 * channels
    return (
        b"RIFF" + struct.pack("<I", 36 + data_bytes) + b"WAVE"
        + b"fmt " + struct.pack(
            "<IHHIIHH", 16, _WAVE_FORMAT_IEEE_FLOAT, channels, sample_rate,
            sample_rate * block, block, 32,
        )
        + b"data" + struct.pack("<I", data_bytes)
    )


def decode_to_wav(path: Path, wav: Path, *, exe: Path | None = None) -> Path:
    """Decode ``path`` with the engine into a 32-bit float stereo WAV at ``wav``.

    Streamed to disk, so peak memory here is one chunk, not the track. For a
    reader that cannot open the source container (Beat This!'s file read on
    an m4a with no ffmpeg); the samples are the engine's, the MP4 edit list
    applied. On any failure ``wav`` is removed, never left half written.
    """
    try:
        return _decode_to_wav(path, wav, exe or resolve_engine_decoder())
    except BaseException:
        wav.unlink(missing_ok=True)
        raise


def _decode_to_wav(path: Path, wav: Path, binary: Path) -> Path:
    with wav.open("wb") as out, tempfile.TemporaryFile() as errors:
        out.write(_wav_header(0, 0, 2))
        try:
            process = subprocess.Popen(  # fixed argv, never a shell
                _decode_argv(binary, path, mono=False),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errors,
            )
        except OSError as exc:
            raise EngineDecoderUnavailable(f"{binary} could not be launched: {exc}") from None
        assert process.stdout is not None  # Popen(stdout=PIPE) always sets it
        # The read loop blocks on a decoder that stalls without closing
        # stdout, so the deadline is a kill from outside, not a wait timeout.
        timed_out = threading.Event()

        def _expire() -> None:
            timed_out.set()
            process.kill()

        watchdog = threading.Timer(DECODE_TIMEOUT_S, _expire)
        watchdog.start()
        written = 0
        try:
            while chunk := process.stdout.read(_CHUNK_BYTES):
                written += len(chunk)
                if written > _WAV_MAX_DATA_BYTES:
                    process.kill()
                    raise EngineDecodeFailed(
                        f"{path} decodes to more than a WAV can hold (4 GiB)"
                    )
                out.write(chunk)
        finally:
            watchdog.cancel()
            process.stdout.close()
            returncode = process.wait()
        if timed_out.is_set():
            raise EngineDecodeFailed(
                f"odj-audio did not decode {path} within {DECODE_TIMEOUT_S}s"
            )
        errors.seek(0)
        stderr = errors.read()
        if returncode != 0:
            tail = stderr.decode("utf-8", "replace").strip().splitlines()
            raise EngineDecodeFailed(
                f"odj-audio could not decode {path}: {tail[-1] if tail else returncode}"
            )
        summary = _summary(stderr)
        if written == 0 or written != 4 * summary["channels"] * summary["frames"]:
            raise EngineDecodeFailed(
                f"odj-audio decoded {written} bytes of {path}, not the "
                f"{summary['frames']} frames its summary states"
            )
        out.seek(0)
        out.write(_wav_header(written, summary["sample_rate"], summary["channels"]))
    return wav


__all__ = [
    "BIN_ENV",
    "SNDFILE_SUFFIXES",
    "EngineDecodeFailed",
    "EngineDecoderUnavailable",
    "decode_f32",
    "decode_to_wav",
    "needs_engine_decode",
    "probe_duration_s",
    "resolve_engine_decoder",
]
