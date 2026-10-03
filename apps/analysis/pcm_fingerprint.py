"""The canonical decode fingerprint every own record is stamped with.

`specs/native-analysis-v1.md` defines `decode_fingerprint` as the sha256 of
the decoded PCM at FIXED parameters: 44100 Hz, mono, s16, decoded and
resampled by the app's own engine, `odj-audio` (fingerprint v2; v1 was
ffmpeg with `soxr`, see `DECODER` below). That is what makes it useful. Two
hosts decoding the same file agree on it, the parity gate compares it across macOS, Linux and Windows,
and the player can check that a served grid came from the same decode it is
about to play.

It is deliberately NOT the hash of whatever a producer happened to feed its
model. Beat This's `load_audio` returns float32 at its own rate, so hashing
that stamps a number no other decoder can reproduce: the comparison the field
exists for fails even when the audio is byte-identical, and it fails in the
direction that looks like corruption rather than like a units mismatch (Codex
P1 BLOCKING, PR #1587). A producer may hash its model input for its own
debugging, but the record carries this one.

The parameters below ARE the contract. Changing any of them changes every
fingerprint in the store, so they move only with a spec change and a
re-backfill, never as a local preference.
"""

from __future__ import annotations

import hashlib
import math
import struct
import subprocess
import tempfile
import threading
import wave
from pathlib import Path

from apps.shared.engine_decode import EngineDecoderUnavailable, resolve_engine_decoder

#-----------------------------------------------------------------------------
# the contract
#-----------------------------------------------------------------------------

DECODER = "odj-audio"
"""The app's own Rust engine (symphonia decode, MP4 edit list applied).

Fingerprint v2 (Fri 2 Oct 2026). v1 was ffmpeg with ``aresample=soxr`` at
precision 28, which the shipped app does not bundle and Homebrew's default
macOS build cannot run, so an installed app took no fingerprint and wrote no
own record (``research/audio-decode/2026-10-01-packaging-audio-decode.md``).
Every payload ships ``odj-audio``, and it is the decoder the Rust player uses,
so the player's check now compares against the decode it plays.
"""
SAMPLE_RATE_HZ = 44100
CHANNELS = 1
SAMPLE_FORMAT = "s16le"
RESAMPLER = "rubato-fft-4096x2"
"""rubato's ``FftFixedIn`` with 4096-frame input chunks and 2 sub-chunks, as
``odj-audio`` resamples on every load (``resample`` in
``apps/audio-engine/src/decode.rs``). Fixed by the engine's source, not by a
host build option, so every host that runs the same engine agrees."""
TIMEOUT_S = 300.0
_CHUNK_BYTES = 1 << 20

#: A positive control for :func:`require_resampler`: 0.1 s of 440 Hz at
#: 48 kHz, which the canonical decode must resample to 44.1 kHz.
_PROBE_RATE_HZ = 48000
_PROBE_SECONDS = 0.1


class FingerprintUnavailable(Exception):
    """The canonical decode did not happen, so there is no fingerprint.

    Never returns a digest of a partial or empty stream in this case: the
    sha256 of zero bytes is a well-formed digest that would sail through every
    validator while proving nothing about the audio.
    """


#-----------------------------------------------------------------------------
# helpers
#-----------------------------------------------------------------------------

def _resolve_or_raise() -> str:
    """The engine lookup (``ODJ_AUDIO_BIN``, else the newest repo build), as
    this module's own failure type."""
    try:
        return str(resolve_engine_decoder())
    except EngineDecoderUnavailable as exc:
        raise FingerprintUnavailable(str(exc)) from None


def _write_probe_tone(path: Path) -> None:
    """A real 16-bit PCM WAV, written with the standard library."""
    frames = int(_PROBE_RATE_HZ * _PROBE_SECONDS)
    samples = (
        int(16000 * math.sin(2 * math.pi * 440 * n / _PROBE_RATE_HZ)) for n in range(frames)
    )
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(_PROBE_RATE_HZ)
        out.writeframes(b"".join(struct.pack("<h", s) for s in samples))


#-----------------------------------------------------------------------------
# public
#-----------------------------------------------------------------------------

def canonical_decode_command(path: Path, exe: str) -> list[str]:
    """The exact argv the fingerprint is defined over.

    Public because the value only means something alongside the parameters it
    was measured under, so callers and reports print this rather than
    paraphrasing it.
    """
    return [
        exe, "decode",
        "--rate", str(SAMPLE_RATE_HZ),
        "--mono",
        "--format", SAMPLE_FORMAT,
        str(path),
    ]


def require_resampler(timeout_s: float = 30.0) -> None:
    """Prove this host can run the canonical decode, resampler included.

    Checked once, before a backfill touches the library: without it EVERY
    record fails its fingerprint, and a host-wide gap reported once per file
    reads as thousands of unreadable tracks. This is a positive control: it
    decodes a real 48 kHz WAV through the exact command the fingerprint uses
    and asserts the expected number of 44.1 kHz s16 bytes comes out, rather
    than checking a version string or the presence of a subcommand.
    """
    exe = _resolve_or_raise()
    with tempfile.TemporaryDirectory() as tmp:
        tone = Path(tmp) / "probe-48k.wav"
        _write_probe_tone(tone)
        try:
            done = subprocess.run(  # fixed argv, never a shell
                canonical_decode_command(tone, exe),
                check=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout_s,
            )
        except OSError as exc:
            raise FingerprintUnavailable(f"{DECODER} could not be launched: {exc}") from None
        except subprocess.TimeoutExpired:
            raise FingerprintUnavailable(
                f"the {DECODER} decode probe did not finish within {timeout_s:.3g}s"
            ) from None
    # The engine's resampler emits exactly round(frames * to / from) frames.
    want = 2 * CHANNELS * round(SAMPLE_RATE_HZ * _PROBE_SECONDS)
    if done.returncode != 0 or len(done.stdout) != want:
        tail = done.stderr.decode("utf-8", "replace").strip().splitlines()
        reason = tail[-1] if tail else f"exit {done.returncode}"
        raise FingerprintUnavailable(
            f"{exe} cannot run the canonical decode ({RESAMPLER} to "
            f"{SAMPLE_RATE_HZ} Hz): {reason}; {len(done.stdout)} bytes out, "
            f"expected {want}"
        )


def canonical_decode_fingerprint(path: Path) -> str:
    """sha256 hex of ``path`` decoded at the fixed parameters above.

    Raises `FingerprintUnavailable` for a missing engine, a launch failure, a
    decode error, a timeout, or an empty stream.
    """
    audio_path = Path(path)
    exe = _resolve_or_raise()
    command = canonical_decode_command(audio_path, exe)
    digest = hashlib.sha256()
    decoded_bytes = 0
    timed_out = threading.Event()
    # stderr to a temp FILE, not a second pipe: nothing drains a second pipe
    # while stdout is being read, so a chatty decoder filling that buffer
    # deadlocks the process forever (apps/analysis_waveform/decode.py).
    with tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen(  # fixed argv, never a shell
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=errors,
            )
        except OSError as exc:
            raise FingerprintUnavailable(f"canonical decoder could not be launched: {exc}") from None
        stdout = process.stdout
        if stdout is None:  # pragma: no cover - Popen(stdout=PIPE) always sets it
            raise FingerprintUnavailable("decoder stdout could not be opened")

        def _kill_on_deadline() -> None:
            timed_out.set()
            process.kill()

        watchdog = threading.Timer(TIMEOUT_S, _kill_on_deadline)
        watchdog.start()
        try:
            # Streamed, so peak memory is one chunk whatever the track length:
            # a 10 minute track is ~53 MB of s16 mono at 44.1 kHz, and the
            # backfill runs this per track.
            while chunk := stdout.read(_CHUNK_BYTES):
                digest.update(chunk)
                decoded_bytes += len(chunk)
        finally:
            watchdog.cancel()
            stdout.close()
            returncode = process.wait()

        if timed_out.is_set():
            raise FingerprintUnavailable(
                f"{DECODER} did not finish decoding {audio_path} within {TIMEOUT_S:.3g}s"
            )
        if returncode != 0:
            errors.seek(0)
            tail = errors.read().decode("utf-8", "replace").strip().splitlines()
            reason = tail[-1] if tail else f"exit {returncode}"
            raise FingerprintUnavailable(f"{DECODER} could not decode {audio_path}: {reason}")
        if decoded_bytes == 0:
            raise FingerprintUnavailable(
                f"{DECODER} decoded 0 bytes of audio from {audio_path}, so there is "
                "nothing to fingerprint"
            )
    return digest.hexdigest()
