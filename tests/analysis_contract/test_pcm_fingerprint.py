"""The canonical decode fingerprint: what it is over, and when it refuses.

In `tests/analysis_contract/`, not `tests/analysis/`, for the reason that
directory's conftest states: it importorskips soundfile at module level and
the fast CI lane ignores it, so a test placed there is a check that cannot
fail. These need the engine (`odj-audio`), not the audio stack.

`decode_fingerprint` exists so a SECOND decoder can recompute it. Every test
here is about that property, not about any particular digest value: the
fingerprint must follow the audio across containers, separate audio that
differs, and refuse rather than return a well-formed digest of nothing.
"""

from __future__ import annotations

import hashlib
import math
import struct
import subprocess
import wave
from pathlib import Path

import pytest

from apps.analysis.pcm_fingerprint import (
    FingerprintUnavailable,
    canonical_decode_command,
    canonical_decode_fingerprint,
    require_resampler,
)
from apps.shared.engine_decode import BIN_ENV, resolve_engine_decoder

#-----------------------------------------------------------------------------
# fixtures
#-----------------------------------------------------------------------------

def _synthesize(
    path: Path, *, hz: int, rate: int, seconds: float = 1.0, width: int = 2
) -> Path:
    """A real PCM WAV, written with the standard library.

    `width` 3 writes the SAME 16-bit samples as 24-bit, shifted up a byte:
    different bytes on disk, identical audio, which is the container change
    the fingerprint must not see.
    """
    frames = int(rate * seconds)
    out = bytearray()
    for n in range(frames):
        s = int(16000 * math.sin(2 * math.pi * hz * n / rate))
        out += struct.pack("<h", s) if width == 2 else struct.pack("<i", s << 8)[:3]
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(bytes(out))
    return path


def _fake_engine(tmp_path: Path, body: str) -> Path:
    """An `odj-audio` stand-in that passes the resolver's subcommand check.

    The resolver asks `help` for the decode and probe usage lines before it
    trusts a binary, so the fake answers that and runs `body` for anything
    else.
    """
    fake = tmp_path / "odj-audio"
    fake.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = help ]; then\n'
        "  echo 'odj-audio decode PATH'; echo 'odj-audio probe PATH'; exit 0\n"
        "fi\n" + body
    )
    fake.chmod(0o755)
    return fake


#-----------------------------------------------------------------------------
# the parameters ARE the contract
#-----------------------------------------------------------------------------

def test_the_command_states_every_pinned_parameter() -> None:
    """A fingerprint means nothing without the parameters it was taken under."""
    command = canonical_decode_command(Path("/x/y.wav"), "/opt/odj-audio")
    assert command == [
        "/opt/odj-audio", "decode", "--rate", "44100", "--mono",
        "--format", "s16le", "/x/y.wav",
    ]


#-----------------------------------------------------------------------------
# refusals: a digest of nothing is still a valid-looking digest
#-----------------------------------------------------------------------------

@pytest.mark.requires_canonical_decode
def test_a_file_that_is_not_audio_is_refused(tmp_path: Path) -> None:
    not_audio = tmp_path / "notes.txt"
    not_audio.write_text("this is not a wav")
    with pytest.raises(FingerprintUnavailable):
        canonical_decode_fingerprint(not_audio)


def test_a_missing_engine_is_refused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(BIN_ENV, str(tmp_path / "nowhere"))
    with pytest.raises(FingerprintUnavailable, match=BIN_ENV):
        canonical_decode_fingerprint(tmp_path / "any.wav")


def test_an_empty_decode_never_returns_the_digest_of_zero_bytes(
    tmp_path: Path, monkeypatch
) -> None:
    """The one failure that would sail through every validator downstream.

    sha256 of an empty stream is a well-formed 64-hex digest. Returned, it
    would satisfy the record contract, the `sha256:` prefix check, and an
    equality comparison between two hosts that both decoded nothing. The
    stub here is a decoder that SUCCEEDS and emits no audio, because a
    nonzero exit is already caught one branch earlier: this is the case where
    only the byte count can tell the difference.
    """
    empty_of_nothing = hashlib.sha256(b"").hexdigest()
    quiet_success = _fake_engine(tmp_path, "exit 0\n")
    monkeypatch.setenv(BIN_ENV, str(quiet_success))

    with pytest.raises(FingerprintUnavailable) as raised:
        canonical_decode_fingerprint(tmp_path / "anything.wav")
    assert empty_of_nothing not in str(raised.value)
    assert "0 bytes" in str(raised.value)


#-----------------------------------------------------------------------------
# the property the field exists for
#-----------------------------------------------------------------------------

@pytest.mark.requires_canonical_decode
def test_the_same_audio_in_two_containers_fingerprints_identically(
    tmp_path: Path,
) -> None:
    """The whole point: the fingerprint follows the DECODE, not the file.

    A 16-bit and a 24-bit WAV of the same samples are different bytes on disk
    and identical PCM once decoded, so a fingerprint that changes here could never
    verify one host's grid against another's decode.
    """
    wav16 = _synthesize(tmp_path / "tone16.wav", hz=440, rate=44100)
    wav24 = _synthesize(tmp_path / "tone24.wav", hz=440, rate=44100, width=3)
    assert wav16.read_bytes() != wav24.read_bytes()
    assert canonical_decode_fingerprint(wav16) == canonical_decode_fingerprint(wav24)


@pytest.mark.requires_canonical_decode
def test_different_audio_fingerprints_differently(tmp_path: Path) -> None:
    """The control that lets the test above mean something."""
    a = _synthesize(tmp_path / "a.wav", hz=440, rate=44100)
    b = _synthesize(tmp_path / "b.wav", hz=880, rate=44100)
    assert canonical_decode_fingerprint(a) != canonical_decode_fingerprint(b)


@pytest.mark.requires_canonical_decode
def test_the_fingerprint_is_a_bare_sha256_hex_digest(tmp_path: Path) -> None:
    tone = _synthesize(tmp_path / "tone.wav", hz=440, rate=44100)
    digest = canonical_decode_fingerprint(tone)
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")
    assert digest == canonical_decode_fingerprint(tone)


@pytest.mark.requires_canonical_decode
def test_it_is_not_the_hash_of_the_model_input(tmp_path: Path) -> None:
    """The defect this module exists to close (Codex P1 BLOCKING, PR #1587).

    The runner hashes Beat This's float32 `load_audio` output at the rate that
    loader chose. Nothing else decodes to those bytes, so a record stamped
    with it fails the cross-host comparison on byte-identical audio.
    """
    import numpy as np

    tone = _synthesize(tmp_path / "tone.wav", hz=440, rate=44100)
    raw = subprocess.run(
        canonical_decode_command(tone, str(resolve_engine_decoder())),
        check=True, stdin=subprocess.DEVNULL, capture_output=True,
    ).stdout
    as_float32 = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    model_input_style = hashlib.sha256(
        np.ascontiguousarray(as_float32).tobytes()
    ).hexdigest()
    assert canonical_decode_fingerprint(tone) != model_input_style


#-----------------------------------------------------------------------------
# the capability probe
#-----------------------------------------------------------------------------

@pytest.mark.requires_canonical_decode
def test_the_probe_passes_where_the_resampler_exists() -> None:
    require_resampler()


def test_the_probe_fails_loud_on_a_build_that_cannot_decode(
    tmp_path: Path, monkeypatch
) -> None:
    """A binary that ANSWERS to the decode subcommand and cannot run it must
    not pass. Listing `decode` in its usage is what the resolver checks, so
    anything short of running a decode reports a capability the host does not
    have.
    """
    fake = _fake_engine(tmp_path, "echo 'cannot resample' >&2\nexit 1\n")
    monkeypatch.setenv(BIN_ENV, str(fake))
    with pytest.raises(FingerprintUnavailable) as raised:
        require_resampler()
    assert "cannot resample" in str(raised.value)


def test_the_probe_fails_loud_on_a_decode_of_the_wrong_length(
    tmp_path: Path, monkeypatch
) -> None:
    """Exit 0 with the wrong byte count is a decoder that did not resample.

    The overshoot control: a probe that only checked the exit code would pass
    a build that streams the 48 kHz source through unconverted.
    """
    fake = _fake_engine(tmp_path, "head -c 9600 /dev/zero\nexit 0\n")
    monkeypatch.setenv(BIN_ENV, str(fake))
    with pytest.raises(FingerprintUnavailable) as raised:
        require_resampler()
    assert "9600 bytes out, expected 8820" in str(raised.value)


#-----------------------------------------------------------------------------
# provenance across the runner
#-----------------------------------------------------------------------------

@pytest.mark.requires_canonical_decode
def test_a_file_replaced_while_the_runner_ran_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record must not vouch for bytes that did not produce its grid.

    The runner takes seconds to minutes per track, and a library-wide backfill
    is exactly when a sync tool, a re-tag or a re-encode lands underneath it.
    Fingerprinting only afterwards would stamp whatever is on disk when the
    model happens to finish, and the resulting record is convincing in the
    worst way: a later cross-host comparison against the NEW bytes agrees,
    while the beats it vouches for came from the old ones (Codex P2 BLOCKING,
    PR #1587).
    """
    from apps.analysis.backends import own_beatgrid
    from apps.analysis.backends.base import TrackVanished

    track = _synthesize(tmp_path / "track.wav", hz=440, rate=44100)
    # `weights` is imported inside `analyze`, not at module scope, so the
    # patch has to land on the source module.
    monkeypatch.setattr(
        "apps.analysis_beatgrid.weights.resolve_checkpoint",
        lambda: ("/checkpoint", "0" * 64),
    )

    def _replace_the_file_mid_run(*args: object, **kwargs: object) -> dict[str, object]:
        _synthesize(track, hz=880, rate=44100)  # different audio, same path
        return {"results": {}}

    monkeypatch.setattr(own_beatgrid, "run_runner", _replace_the_file_mid_run)
    with pytest.raises(TrackVanished) as raised:
        own_beatgrid.OwnBeatgridBackfillBackend.analyze(track, "sid-replaced")
    assert "changed while the runner" in str(raised.value)


@pytest.mark.requires_canonical_decode
def test_an_untouched_file_gets_past_the_provenance_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: the guard must not be failing for everyone.

    A guard that rejected every track would satisfy the test above perfectly.
    Here the runner leaves the file alone and returns a payload that is
    malformed for a DIFFERENT reason, so reaching that second failure proves
    the fingerprint comparison passed rather than never having run.
    """
    from apps.analysis.backends import own_beatgrid
    from apps.analysis.backends.base import TrackVanished

    track = _synthesize(tmp_path / "track.wav", hz=440, rate=44100)
    # `weights` is imported inside `analyze`, not at module scope, so the
    # patch has to land on the source module.
    monkeypatch.setattr(
        "apps.analysis_beatgrid.weights.resolve_checkpoint",
        lambda: ("/checkpoint", "0" * 64),
    )
    monkeypatch.setattr(
        own_beatgrid, "run_runner", lambda *a, **k: {"results": {}}
    )
    with pytest.raises(Exception) as raised:
        own_beatgrid.OwnBeatgridBackfillBackend.analyze(track, "sid-untouched")
    assert not isinstance(raised.value, TrackVanished), (
        "the file was never touched, so the provenance guard must not be what fires"
    )
