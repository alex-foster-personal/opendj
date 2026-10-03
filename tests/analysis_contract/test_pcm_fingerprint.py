"""The canonical decode fingerprint: what it is over, and when it refuses.

In `tests/analysis_contract/`, not `tests/analysis/`, for the reason that
directory's conftest states: it importorskips soundfile at module level and
the fast CI lane ignores it, so a test placed there is a check that cannot
fail. These need the engine (`odj-audio`), not the audio stack.

`decode_fingerprint` exists so a SECOND decoder can recompute it. Every
fingerprint test here is about that property, not about any particular digest
value: the fingerprint must follow the audio across containers, separate audio
that differs, and refuse rather than return a well-formed digest of nothing.
"""

from __future__ import annotations

import hashlib
import math
import os
import shutil
import struct
import subprocess
import sys
import wave
from contextlib import AbstractContextManager
from pathlib import Path

import pytest

from apps.analysis.backends import own_beatgrid, own_beatgrid_input, own_key
from apps.analysis.backends.base import BackendNotAvailable, TrackUnreadable, TrackVanished
from apps.analysis.backends.own_beatgrid import OwnBeatgridBackfillBackend
from apps.analysis.pcm_fingerprint import (
    FingerprintUnavailable,
    canonical_decode_command,
    canonical_decode_fingerprint,
    require_resampler,
)
from apps.shared import engine_decode
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


# The stand-in below is a shebang script, which Windows cannot execute.
_POSIX_STAND_IN = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the stand-in odj-audio is a POSIX shell script; Windows cannot run a shebang",
)


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


@_POSIX_STAND_IN
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


@_POSIX_STAND_IN
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


@_POSIX_STAND_IN
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


#-----------------------------------------------------------------------------
# engine decode lanes (NAE-23)
#-----------------------------------------------------------------------------

# The beatgrid and key lanes read an m4a through the engine (NAE-23).
# Neither lane's own reader opens an m4a without ffmpeg (Beat This! reads via
# torchaudio, soundfile and madmom; the key lane via librosa and audioread), and
# the shipped app has no ffmpeg.
#
# Skips as UNAVAILABLE on a host with no odj-audio build; ci.yml runs this
# file where the engine was just built, where a missing build fails instead.
# Each test in this section carries the mark on its own, because the
# fingerprint tests above must not all carry requires_canonical_decode.

_REPO = Path(__file__).resolve().parents[2]
_FIXTURES = _REPO / "apps/audio-engine/tests/fixtures/audio"
_M4A = _FIXTURES / "click-250ms-aac.m4a"
# 16 s of a synthesized 124 BPM drum loop (kick, snare on 2 and 4, offbeat
# hats, a bass note per bar), encoded once with `ffmpeg -c:a aac -b:a 32k -ac 1`.
# A bare click track has no bar phase for the runner to anchor.
_LOOP_M4A = _FIXTURES / "loop-124bpm-16s-aac.m4a"
# Version 1 of that fixture. A changed file is a new fixture, never a quiet swap.
_LOOP_M4A_SHA256 = "1c17d701d2d39aec459ceac2885e44b8a42271b30e9401cd1c1f484e4f13cab6"


def _input(track: Path, decode_dir: Path) -> AbstractContextManager[Path]:
    return own_beatgrid_input.runner_input(track, decode_dir=decode_dir)


def _wavs(folder: Path) -> list[Path]:
    return list(folder.rglob("*.wav"))


def _in_child(code: str, *args: object, **env: str) -> str:
    """Run ``code`` in a real interpreter configured by ``env``; its stdout.

    Configuration crosses a real process boundary, the way the shipped app
    sets it, so nothing in this process is patched.
    """
    done = subprocess.run(
        [sys.executable, "-c", code, *map(str, args)],
        env={**os.environ, **env}, cwd=_REPO,
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def _hydrated_loop(folder: Path) -> Path:
    """A disposable copy of the loop fixture, checked against its recorded digest."""
    track = folder / "Loop.m4a"
    shutil.copy(_LOOP_M4A, track)
    digest = hashlib.sha256(track.read_bytes()).hexdigest()
    assert digest == _LOOP_M4A_SHA256, f"{_LOOP_M4A.name} is not fixture v1: {digest}"
    return track


@pytest.mark.requires_canonical_decode
def test_the_loop_fixture_is_the_recorded_one(tmp_path: Path) -> None:
    _hydrated_loop(tmp_path)


@pytest.mark.requires_canonical_decode
def test_the_runner_reads_a_wav_at_the_same_path_every_run(tmp_path: Path) -> None:
    soundfile = pytest.importorskip("soundfile")
    track = tmp_path / "Song.m4a"
    shutil.copy(_M4A, track)
    seen = []
    for _ in range(2):
        with _input(track, tmp_path / "decode") as wav:
            assert soundfile.info(str(wav)).frames == 44100
            seen.append(wav)
    # The runner names its activation file after this path, so it must not move.
    assert seen[0] == seen[1] and seen[0].name == "Song.wav"
    assert not _wavs(tmp_path / "decode")


@pytest.mark.requires_canonical_decode
def test_the_runner_reads_an_mp3_as_itself(tmp_path: Path) -> None:
    track = tmp_path / "Song.mp3"
    with _input(track, tmp_path) as runner_input:
        assert runner_input == track


@pytest.mark.requires_canonical_decode
def test_a_second_run_of_the_same_track_waits_rather_than_clobbering(tmp_path: Path) -> None:
    track = tmp_path / "Song.m4a"
    shutil.copy(_M4A, track)
    with _input(track, tmp_path / "decode") as wav:
        before = wav.read_bytes()
        with (
            pytest.raises(TrackVanished, match="another run"),
            _input(track, tmp_path / "decode"),
        ):
            pass
        # The refused second run left the first run's WAV untouched.
        assert wav.read_bytes() == before
    # ...and once the first run is done, the track is free again.
    with _input(track, tmp_path / "decode") as wav:
        assert wav.is_file()


@pytest.mark.requires_canonical_decode
def test_a_run_that_died_holding_the_lock_does_not_block_the_track(tmp_path: Path) -> None:
    track = tmp_path / "Song.m4a"
    shutil.copy(_M4A, track)
    with _input(track, tmp_path / "decode") as wav:
        lock = wav.parent / ".lock"
    holder = subprocess.Popen(  # a real second process holding the real lock
        [sys.executable, "-c", (
            "import os, sys, time\n"
            "from apps.analysis.backends.own_beatgrid_input import _try_lock\n"
            "fd = os.open(sys.argv[1], os.O_RDWR)\n"
            "assert _try_lock(fd)\n"
            "print('held', flush=True)\n"
            "time.sleep(600)\n"
        ), str(lock)],
        stdout=subprocess.PIPE, text=True, cwd=Path(__file__).resolve().parents[2],
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        with pytest.raises(TrackVanished, match="another run"), _input(track, tmp_path / "decode"):
            pass
    finally:
        holder.kill()
        holder.wait()
    # The OS dropped the dead holder's lock; no stale-lock takeover was needed.
    with _input(track, tmp_path / "decode") as wav:
        assert wav.is_file()


@pytest.mark.requires_canonical_decode
def test_the_runner_wav_is_deleted_when_the_runner_cannot_start(tmp_path: Path) -> None:
    # The runner interpreter really does not exist, so the launch really fails.
    out = _in_child(
        "import sys\n"
        "from pathlib import Path\n"
        "from apps.analysis.backends import own_beatgrid\n"
        "from apps.analysis.backends.base import BackendNotAvailable\n"
        "try:\n"
        "    own_beatgrid.run_runner(Path(sys.argv[1]), Path(sys.argv[2]), device='cpu')\n"
        "except BackendNotAvailable as exc:\n"
        "    print('unavailable:', exc)\n",
        _M4A, tmp_path / "ckpt",
        MDT_BEATGRID_ACTIVATIONS_DIR=str(tmp_path / "activations"),
        **{own_beatgrid.RUNNER_PYTHON_ENV: str(tmp_path / "no-python")},
    )
    assert out.startswith("unavailable:") and "could not launch" in out, out
    decode_dir = tmp_path / "engine-decode"
    assert decode_dir.is_dir(), "the engine decode never ran, so this proves nothing"
    assert not _wavs(decode_dir)


@pytest.mark.requires_canonical_decode
def test_the_key_lane_reads_an_m4a_through_the_engine() -> None:
    samples, rate = own_key._decode(_M4A)
    pcm, engine_rate, _ = engine_decode.decode_f32(_M4A, mono=True)
    # Exactly the engine's samples: 44100 frames with the AAC priming dropped,
    # where librosa through ffmpeg returns 45056 with it kept.
    assert rate == engine_rate == 44100
    assert samples.tobytes() == pcm and samples.shape == (44100,)
    # A view of the engine's bytes, not a second whole-track copy.
    assert not samples.flags.owndata
    assert abs(int(abs(samples).argmax()) - 11025) < 64


@pytest.mark.requires_canonical_decode
@pytest.mark.parametrize("lane", ["beatgrid", "key"])
def test_an_unreadable_m4a_is_one_bad_track(tmp_path: Path, lane: str) -> None:
    junk = tmp_path / "junk.m4a"
    junk.write_bytes(b"not audio" * 256)
    with pytest.raises(TrackUnreadable, match="could not decode"):
        if lane == "beatgrid":
            with _input(junk, tmp_path / "decode"):
                pass
        else:
            own_key._decode(junk)
    if lane == "beatgrid":
        assert not _wavs(tmp_path / "decode")


@pytest.mark.requires_canonical_decode
@pytest.mark.parametrize("lane", ["beatgrid", "key"])
def test_a_missing_engine_stops_the_lane(tmp_path: Path, lane: str) -> None:
    out = _in_child(
        "import sys\n"
        "from pathlib import Path\n"
        "from apps.analysis.backends import own_beatgrid_input, own_key\n"
        "from apps.analysis.backends.base import BackendNotAvailable\n"
        "track, decode_dir = Path(sys.argv[1]), Path(sys.argv[2])\n"
        "try:\n"
        "    if sys.argv[3] == 'beatgrid':\n"
        "        with own_beatgrid_input.runner_input(track, decode_dir=decode_dir):\n"
        "            pass\n"
        "    else:\n"
        "        own_key._decode(track)\n"
        "except BackendNotAvailable as exc:\n"
        "    print('unavailable:', exc)\n",
        _M4A, tmp_path / "decode", lane,
        **{BIN_ENV: str(tmp_path / "absent")},
    )
    assert out.startswith("unavailable:") and "absent" in out, out


@pytest.mark.requires_canonical_decode
@pytest.mark.skipif(
    os.environ.get("MDT_BEATGRID_MODEL_TESTS") != "1",
    reason=(
        "drives the real Beat This! runner, which needs uv and a provisioned "
        "checkpoint (see test_anlz_own_beatgrid_real_analyzer); "
        "set MDT_BEATGRID_MODEL_TESTS=1 to run"
    ),
)
def test_the_real_beatgrid_runner_reads_an_m4a(tmp_path: Path) -> None:
    track = _hydrated_loop(tmp_path)
    try:
        record = OwnBeatgridBackfillBackend.analyze(track, "m4a-acceptance")
    except BackendNotAvailable as exc:
        pytest.fail(f"MDT_BEATGRID_MODEL_TESTS=1 but the runner is unavailable: {exc}")
    lane = record.lanes["beatgrid"]
    assert lane.status == "ok", lane
    assert lane.payload["beats"], "the real runner found no beats in the m4a"
    assert abs(record.bpm - 124.0) < 1.0, f"read {record.bpm} BPM off a 124 BPM loop"
