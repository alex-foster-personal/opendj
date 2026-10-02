"""The beatgrid and key lanes read an m4a through the engine (NAE-22).

Neither lane's own reader opens an m4a without ffmpeg (Beat This! reads via
torchaudio, soundfile and madmom; the key lane via librosa and audioread), and
the shipped app has no ffmpeg.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from contextlib import AbstractContextManager
from pathlib import Path

import pytest

from apps.analysis.backends import own_beatgrid, own_beatgrid_input, own_key
from apps.analysis.backends.base import TrackUnreadable, TrackVanished
from apps.shared.engine_decode import BIN_ENV

_REPO = Path(__file__).resolve().parents[2]
_FIXTURES = _REPO / "apps/audio-engine/tests/fixtures/audio"
_M4A = _FIXTURES / "click-250ms-aac.m4a"
# 16 s of a synthesized 124 BPM drum loop (kick, snare on 2 and 4, offbeat
# hats, a bass note per bar), encoded once with `ffmpeg -c:a aac -b:a 32k -ac 1`.
# A bare click track has no bar phase for the runner to anchor.
_LOOP_M4A = _FIXTURES / "loop-124bpm-16s-aac.m4a"


# Skips as UNAVAILABLE on a host with no odj-audio build; ci.yml runs this
# file where the engine was just built, where a missing build fails instead.
pytestmark = pytest.mark.requires_canonical_decode



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


def test_the_runner_reads_an_mp3_as_itself(tmp_path: Path) -> None:
    track = tmp_path / "Song.mp3"
    with _input(track, tmp_path) as runner_input:
        assert runner_input == track


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


def test_the_key_lane_reads_an_m4a_through_the_engine() -> None:
    from apps.shared import engine_decode

    samples, rate = own_key._decode(_M4A)
    pcm, engine_rate, _ = engine_decode.decode_f32(_M4A, mono=True)
    # Exactly the engine's samples: 44100 frames with the AAC priming dropped,
    # where librosa through ffmpeg returns 45056 with it kept.
    assert rate == engine_rate == 44100
    assert samples.tobytes() == pcm and samples.shape == (44100,)
    # A view of the engine's bytes, not a second whole-track copy.
    assert not samples.flags.owndata
    assert abs(int(abs(samples).argmax()) - 11025) < 64


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


@pytest.mark.skipif(
    os.environ.get("MDT_BEATGRID_MODEL_TESTS") != "1",
    reason=(
        "drives the real Beat This! runner, which needs uv and a provisioned "
        "checkpoint (see test_anlz_own_beatgrid_real_analyzer); "
        "set MDT_BEATGRID_MODEL_TESTS=1 to run"
    ),
)
def test_the_real_beatgrid_runner_reads_an_m4a(tmp_path: Path) -> None:
    from apps.analysis.backends.base import BackendNotAvailable
    from apps.analysis.backends.own_beatgrid import OwnBeatgridBackfillBackend

    track = tmp_path / "Loop.m4a"
    shutil.copy(_LOOP_M4A, track)
    try:
        record = OwnBeatgridBackfillBackend.analyze(track, "m4a-acceptance")
    except BackendNotAvailable as exc:
        pytest.fail(f"MDT_BEATGRID_MODEL_TESTS=1 but the runner is unavailable: {exc}")
    lane = record.lanes["beatgrid"]
    assert lane.status == "ok", lane
    assert lane.payload["beats"], "the real runner found no beats in the m4a"
    assert abs(record.bpm - 124.0) < 1.0, f"read {record.bpm} BPM off a 124 BPM loop"
