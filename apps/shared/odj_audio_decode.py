"""Decode compressed audio to a temporary WAV through ``odj-audio decode``.

WHY THIS EXISTS. The stems and vocals workers read non-WAV audio through
ffmpeg (demucs' ``AudioFile`` shells to it). The installed app ships no
ffmpeg, by decision (no GPL or LGPL binary in the bundle), so a LOCAL stems
job on an MP3 died with "non-WAV input ... needs ffmpeg on PATH" in the
signed app (packaged check of 316572f5, Fri 2 Oct 2026, finding 3), and
1246 of that library's 1274 tracks are MP3. The app does ship ``odj-audio``,
the Rust engine, whose symphonia decoders (MPL-2.0) read every format a deck
loads; its ``decode`` subcommand writes a float WAV of a source at the
source's own rate and channel count. This module is the workers' one route
to it (``docs/decisions/*-odj-audio-decode-for-workers.md``).

ffmpeg stays first wherever it resolves, so a development machine decodes
exactly as before; ``odj-audio`` is the fallback when it does not.

Imports nothing beyond the standard library and the shared binary resolver
(``apps.shared.odj_audio_binary``, the engine supervisor's own rule), because
the workers that use it run in their own torch environment, not the repo venv.

Requirements (mini-PRD, STEM-49 and STEM-50):
  [if] ffmpeg is absent and ``ODJ_AUDIO_BIN`` names the engine [then] a
    non-WAV source decodes through ``odj-audio decode`` into a temp dir
    outside the app bundle
  [if] neither ffmpeg nor odj-audio is reachable [then ⛔️] the error names
    the file, MDT_FFMPEG/PATH and ODJ_AUDIO_BIN
  [if] ``ODJ_AUDIO_BIN`` is set but wrong [then ⛔️] that is the answer: no
    repo build is used instead (the packaged-app rule of ``resolve_binary``)
  [if] ``odj-audio decode`` fails or its summary disagrees with the file it
    wrote [then ⛔️] raise with its stderr, never hand back a partial WAV

-Claude
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from apps.shared.odj_audio_binary import BIN_ENV, OdjAudioUnavailable, find_binary

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
"""The checkout (or ``payload/app``) a repo-built engine is looked for under.

Only reached when ``ODJ_AUDIO_BIN`` is unset: the packaged launcher always
sets it, so the payload never looks here.
"""

DECODE_TIMEOUT_S: float = 600.0
"""A whole track decodes in well under a second per minute of audio; ten
minutes means the process is wedged, not slow."""


class NoDecoderError(RuntimeError):
    """Neither ffmpeg nor odj-audio can decode this file here."""


class OdjAudioDecodeError(RuntimeError):
    """``odj-audio decode`` ran and failed, or wrote something unexpected."""


@dataclass(frozen=True)
class DecodedWav:
    path: Path
    sample_rate: int
    channels: int
    frames: int

    @property
    def duration_s(self) -> float:
        return self.frames / self.sample_rate


def no_decoder_message(audio_path: Path, why_not_odj_audio: str) -> str:
    """The one sentence both workers raise when nothing can decode the file."""
    return (
        f"non-WAV input {audio_path} cannot be decoded here: ffmpeg is "
        f"not on PATH and MDT_FFMPEG is unset, and odj-audio is unavailable "
        f"({why_not_odj_audio}). Fix one of: (1) set {BIN_ENV} to the "
        f"odj-audio engine (the installed app's launcher does), or build it "
        f"with `cargo build --release --manifest-path "
        f"apps/audio-engine/Cargo.toml`; (2) put ffmpeg on PATH or set "
        f"MDT_FFMPEG; (3) pre-transcode the file to WAV (WAV needs neither)."
    )


def resolve_odj_audio(
    audio_path: Path,
    environ: Mapping[str, str] | None = None,
    repo_root: Path | None = None,
) -> Path:
    """The ``odj-audio`` binary to decode ``audio_path`` with, or raise.

    ``ODJ_AUDIO_BIN`` wins and is never second-guessed, exactly as the engine
    supervisor resolves it; without it, the newest local cargo build.
    """
    env = os.environ if environ is None else environ
    try:
        return find_binary(env, repo_root or REPO_ROOT).path
    except OdjAudioUnavailable as exc:
        raise NoDecoderError(no_decoder_message(audio_path, str(exc))) from exc


def ffmpeg_on_path(environ: Mapping[str, str] | None = None) -> bool:
    """Whether ``MDT_FFMPEG`` is set or ``ffmpeg`` is on PATH: the stems
    worker's lookup (it hands ``MDT_FFMPEG`` to ffmpeg as given)."""
    env = os.environ if environ is None else environ
    if env.get("MDT_FFMPEG"):
        return True
    return shutil.which("ffmpeg", path=env.get("PATH", os.defpath)) is not None


@contextmanager
def decoded_wav(audio_path: Path, *, prefix: str) -> Iterator[DecodedWav]:
    """``audio_path`` decoded by odj-audio into a system temp dir that is
    removed on exit. Never under the app bundle: a file added there breaks
    its code signature (STEM-49). Raises :class:`NoDecoderError` when no
    odj-audio resolves, before anything is written."""
    binary = resolve_odj_audio(audio_path)
    with tempfile.TemporaryDirectory(prefix=prefix) as tmp:
        yield decode_to_wav(audio_path, Path(tmp), binary=binary)


def decode_to_wav(audio_path: Path, out_dir: Path, *, binary: Path) -> DecodedWav:
    """Run ``odj-audio decode`` on ``audio_path`` into a new WAV in ``out_dir``.

    ``out_dir`` is the caller's temporary directory (never the app bundle);
    the WAV is created there and never replaces an existing file.
    """
    out = out_dir / f"{audio_path.stem}.decoded.wav"
    proc = subprocess.run(
        [str(binary), "decode", "--in", str(audio_path), "--out", str(out)],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
        timeout=DECODE_TIMEOUT_S,
    )
    if proc.returncode != 0:
        raise OdjAudioDecodeError(
            f"odj-audio decode failed for {audio_path} (rc={proc.returncode}): "
            f"{proc.stderr.strip()[-800:] or 'no stderr'}"
        )
    try:
        summary = json.loads(proc.stdout.strip().splitlines()[-1])
        decoded = DecodedWav(
            path=Path(summary["out"]),
            sample_rate=int(summary["sample_rate"]),
            channels=int(summary["channels"]),
            frames=int(summary["frames"]),
        )
    except (IndexError, ValueError, KeyError, TypeError) as exc:
        raise OdjAudioDecodeError(
            f"odj-audio decode for {audio_path} printed no usable summary: "
            f"{proc.stdout.strip()[-400:]!r}"
        ) from exc
    expected_bytes = 44 + decoded.frames * decoded.channels * 4
    if decoded.path != out or not out.is_file() or out.stat().st_size != expected_bytes:
        raise OdjAudioDecodeError(
            f"odj-audio decode for {audio_path} reported {decoded.frames} frames x "
            f"{decoded.channels} ch at {decoded.path}, which is not the file on disk"
        )
    if decoded.sample_rate <= 0 or decoded.frames <= 0:
        raise OdjAudioDecodeError(f"odj-audio decode for {audio_path} wrote no audio")
    return decoded


__all__ = [
    "BIN_ENV",
    "DECODE_TIMEOUT_S",
    "DecodedWav",
    "NoDecoderError",
    "OdjAudioDecodeError",
    "decode_to_wav",
    "decoded_wav",
    "ffmpeg_on_path",
    "no_decoder_message",
    "resolve_odj_audio",
]
