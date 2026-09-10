"""The canonical decode fingerprint every own record is stamped with.

`specs/native-analysis-v1.md` defines `decode_fingerprint` as the sha256 of
the decoded PCM at FIXED parameters: 44100 Hz, mono, s16, resampler `soxr` at
a pinned precision. That is what makes it useful. Two hosts decoding the same
file agree on it, the parity gate compares it across macOS, Linux and Windows,
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
import subprocess
import tempfile
import threading
from pathlib import Path

from apps.analysis_waveform.decode import LocalDecodeUnavailable, resolve_ffmpeg

#-----------------------------------------------------------------------------
# the contract
#-----------------------------------------------------------------------------

SAMPLE_RATE_HZ = 44100
CHANNELS = 1
SAMPLE_FORMAT = "s16le"
RESAMPLER = "soxr"
# soxr's own default precision has moved between ffmpeg releases, so the
# number is stated rather than inherited: an inherited default is a parameter
# that changes when the host's ffmpeg changes, which is the one thing this
# fingerprint may not do.
RESAMPLER_PRECISION = 28
TIMEOUT_S = 300.0
_CHUNK_BYTES = 1 << 20


_RESAMPLE_FILTER = (
    f"aresample={SAMPLE_RATE_HZ}:resampler={RESAMPLER}:precision={RESAMPLER_PRECISION}"
)


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
    try:
        return resolve_ffmpeg()
    except LocalDecodeUnavailable as exc:
        raise FingerprintUnavailable(str(exc)) from None


#-----------------------------------------------------------------------------
# public
#-----------------------------------------------------------------------------

def canonical_decode_command(path: Path, exe: str) -> list[str]:
    """The exact ffmpeg argv the fingerprint is defined over.

    Public because the value only means something alongside the parameters it
    was measured under, so callers and reports print this rather than
    paraphrasing it.
    """
    return [
        exe, "-nostdin", "-v", "error",
        "-i", str(path),
        "-vn", "-map", "0:a:0",
        "-ac", str(CHANNELS),
        "-af", _RESAMPLE_FILTER,
        "-f", SAMPLE_FORMAT, "-acodec", "pcm_s16le",
        "-",
    ]


def require_resampler(timeout_s: float = 30.0) -> None:
    """Prove this host's ffmpeg can actually run the pinned resampler.

    `soxr` is a BUILD option, not a runtime one: the `resampler=soxr` value is
    listed in `-h full` on every ffmpeg, and a build without libsoxr accepts
    the argument and then fails at filter-configure time with "Requested
    resampling engine is unavailable". Homebrew's ffmpeg 9.0.1 on macOS is
    such a build, measured Wed 9 Sep 2026, and it fails for a 44100 Hz source
    too, so nothing rescues it - a fingerprint pass on this host would be zero
    tracks, per track, with a decode error each time.

    So the capability is proven ONCE, on a synthetic tone, before a backfill
    touches the library. This is a positive control: it runs the real filter
    chain the fingerprint uses and asserts bytes come out, rather than
    checking a version string or the presence of an option name.
    """
    exe = _resolve_or_raise()
    probe = [
        exe, "-nostdin", "-v", "error",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=0.1:sample_rate=48000",
        "-ac", str(CHANNELS),
        "-af", _RESAMPLE_FILTER,
        "-f", SAMPLE_FORMAT, "-acodec", "pcm_s16le",
        "-",
    ]
    try:
        done = subprocess.run(  # fixed argv, never a shell
            probe,
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout_s,
        )
    except OSError as exc:
        raise FingerprintUnavailable(f"ffmpeg could not be launched: {exc}") from None
    except subprocess.TimeoutExpired:
        raise FingerprintUnavailable(
            f"the ffmpeg resampler probe did not finish within {timeout_s:.3g}s"
        ) from None
    if done.returncode != 0 or not done.stdout:
        tail = done.stderr.decode("utf-8", "replace").strip().splitlines()
        reason = tail[-1] if tail else f"exit {done.returncode}, {len(done.stdout)} bytes out"
        raise FingerprintUnavailable(
            f"{exe} cannot run the pinned resampler ({RESAMPLER}): {reason}. "
            "decode_fingerprint is defined over that resampler, so a build "
            "without it cannot produce a comparable digest. Install an ffmpeg "
            "built --enable-libsoxr (Homebrew's default macOS build is not) or "
            "point MDT_FFMPEG at one."
        )


def canonical_decode_fingerprint(path: Path) -> str:
    """sha256 hex of ``path`` decoded at the fixed parameters above.

    Raises `FingerprintUnavailable` for a missing ffmpeg, a launch failure, a
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
            raise FingerprintUnavailable(f"ffmpeg could not be launched: {exc}") from None
        stdout = process.stdout
        if stdout is None:  # pragma: no cover - Popen(stdout=PIPE) always sets it
            raise FingerprintUnavailable("ffmpeg stdout could not be opened")

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
                f"ffmpeg did not finish decoding {audio_path} within {TIMEOUT_S:.3g}s"
            )
        if returncode != 0:
            errors.seek(0)
            tail = errors.read().decode("utf-8", "replace").strip().splitlines()
            reason = tail[-1] if tail else f"exit {returncode}"
            raise FingerprintUnavailable(f"ffmpeg could not decode {audio_path}: {reason}")
        if decoded_bytes == 0:
            raise FingerprintUnavailable(
                f"ffmpeg decoded 0 bytes of audio from {audio_path}, so there is "
                "nothing to fingerprint"
            )
    return digest.hexdigest()
