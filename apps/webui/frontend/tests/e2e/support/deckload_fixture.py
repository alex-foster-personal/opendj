"""Build the webkit performance-suite fixture library. No mocked data.

The webkit suite drives the PRODUCTION build served by the engine daemon, so
it needs a library the engine can actually serve. It cannot use the lane data
dir (one engine holds a singleton lock on it) and it must never touch the
primary checkout's ``data/``. This module builds a throwaway one instead:

1. Real audio is GENERATED here (stdlib ``wave`` only): a stereo pulse train
   at a known BPM under a strong sustained tone, so decode, playback,
   position measurement AND spectral measurement all have real signal to
   work on. See "spectral landmark" below for why the tone is where it is.
2. The library rows are written by the REAL folder ingest
   (``python -m apps.shared.state.cli ingest-folder``), not by hand. That
   adapter reads tags and writes NO bpm, NO key and NO beatgrid, and says so.
   The engine has a first-class path for exactly those rows
   (``local_anlz_payload``: "a deck can load and play with no grid").

What that honestly leaves OUT is named rather than faked: these tracks have no
rekordbox vendor mapping, so ``/anlz`` serves a payload whose beatgrid, cues
and phrases are all empty. Since Tue 1 Sep 2026 its WAVEFORM is not: those
peaks come from our own ffmpeg decode of the fixture audio (PARITY-03), so a
lane drawn here describes the real tone. The only in-repo beatgrid producer
for arbitrary audio is ``apps.analysis`` (librosa+madmom), which is
deliberately absent from the repo venv, so nothing here invents a grid.

SPECTRAL LANDMARK. The tempo, key and master-tempo tests assert the effect on
the AUDIO, read back through the app's own post-processor AnalyserNode. That
analyser is 4096-point at 44.1 kHz, so its bins are ~10.8 Hz wide. The tone
therefore sits at 2 kHz: a +-16% tempo move shifts it ~30 bins and a
one-semitone key nudge ~11 bins, which is an unambiguous read. The original
220 Hz bed moved only ~1.6 bins under the same change, which is too fragile to
assert on honestly, so it was raised rather than asserted loosely.

Idempotent: re-running with an existing, verified fixture is a no-op, because
Playwright evaluates its config (and this builder) once per process. The
generated audio is versioned by ``FIXTURE_REVISION``: bumping it wipes and
regenerates a stale fixture dir instead of silently measuring old audio.

Usage::

    uv run --no-sync python -m apps.webui.frontend.tests.e2e.support.deckload_fixture \
        --data-dir /abs/path/to/tests/e2e/fixtures/deckload-data --seed-playlists

Acceptance tests:

- [if] the ingest writes zero tracks [then] the builder exits non-zero.
- [if] a generated wav is header-only (no PCM frames) [then] it exits non-zero.
- [if] --data-dir is relative [then] it exits non-zero before writing anything.
- [if] the dir was built by an older FIXTURE_REVISION [then] the audio and the
  state db are wiped and rebuilt, never reused.
- [if] --seed-playlists is set [then] one populated and one empty playlist are
  written through StateWriter, never direct SQLite inserts.
"""

from __future__ import annotations

import argparse
import array
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import wave
from dataclasses import dataclass
from pathlib import Path

from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter

REPOSITORY_ROOT: Path = Path(__file__).resolve().parents[6]

#: Bump to invalidate every generated fixture dir. Anything that changes what
#: the audio SOUNDS like must bump this, or a stale dir keeps being measured.
FIXTURE_REVISION: int = 3
REVISION_MARKER: str = "fixture-revision.txt"

SAMPLE_RATE_HZ: int = 44_100
CHANNELS: int = 2
SAMPLE_WIDTH_BYTES: int = 2

#: The spectral landmark the tempo/key/master-tempo assertions measure. Loud
#: and continuous so it is the dominant bin of the analyser's spectrum, and
#: high enough that a percentage shift moves it many bins (see module docstring).
TONE_HZ: float = 2_000.0
TONE_PEAK: float = 0.50

#: Beat transient. Deliberately far BELOW the measurement band so a broadband
#: onset can never be mistaken for the shifted tone.
PULSE_HZ: float = 100.0
PULSE_MS: float = 40.0
PULSE_PEAK: float = 0.35

#: The band the suite searches for the dominant bin. Wide enough to hold the
#: tone under any range this suite drives (-1 semitone to +16% is 1888..2320 Hz),
#: narrow enough to exclude the pulse fundamental and its low harmonics.
MEASUREMENT_BAND_HZ: tuple[float, float] = (800.0, 8_000.0)


@dataclass(frozen=True)
class FixtureTrack:
    """One generated file. ``bpm`` is a property of the audio, not metadata."""

    filename: str
    bpm: float
    seconds: float


#: Two tracks, because the suite loads deck 1 AND deck 2 and a single shared
#: file would hide any per-deck state bleed. Both are long enough that a
#: transport test can play, pause and seek without hitting the end.
FIXTURE_TRACKS: tuple[FixtureTrack, ...] = (
    FixtureTrack(filename="webkit-fixture-a-128bpm.wav", bpm=128.0, seconds=60.0),
    FixtureTrack(filename="webkit-fixture-b-124bpm.wav", bpm=124.0, seconds=60.0),
)

AUDIO_SUBDIR: str = "fixture-audio"

POPULATED_PLAYLIST_ID: str = "e2e-fixture-populated"
EMPTY_PLAYLIST_ID: str = "e2e-fixture-empty"


# ----- audio generation ------------------------------------------------------
def _pulse_envelope(sample_index: int, beat_period_samples: float) -> float:
    """Linear-decay envelope for the transient that starts on each beat."""
    pulse_len = SAMPLE_RATE_HZ * PULSE_MS / 1000.0
    since_beat = sample_index % beat_period_samples
    if since_beat >= pulse_len:
        return 0.0
    return 1.0 - (since_beat / pulse_len)


def write_tone_and_pulse_wav(path: Path, track: FixtureTrack) -> int:
    """Write one stereo 16-bit fixture file. Returns the PCM frame count.

    A continuous TONE_HZ sine (the thing the spectral assertions measure) plus
    a PULSE_HZ transient on every beat (so the file behaves like a track with
    onsets rather than a test tone).
    """
    frame_count = round(track.seconds * SAMPLE_RATE_HZ)
    beat_period_samples = SAMPLE_RATE_HZ * 60.0 / track.bpm
    peak = float(2**15 - 1)
    tone_step = 2.0 * math.pi * TONE_HZ / SAMPLE_RATE_HZ
    pulse_step = 2.0 * math.pi * PULSE_HZ / SAMPLE_RATE_HZ
    samples = array.array("h")
    for n in range(frame_count):
        value = TONE_PEAK * math.sin(tone_step * n)
        env = _pulse_envelope(n, beat_period_samples)
        if env > 0.0:
            value += PULSE_PEAK * env * math.sin(pulse_step * n)
        sample = int(max(-1.0, min(1.0, value)) * peak)
        samples.append(sample)
        samples.append(sample)
    if sys.byteorder != "little":
        samples.byteswap()
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(CHANNELS)
        handle.setsampwidth(SAMPLE_WIDTH_BYTES)
        handle.setframerate(SAMPLE_RATE_HZ)
        handle.writeframes(samples.tobytes())
    return frame_count


def _wav_frame_count(path: Path) -> int:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes()


def ensure_audio(audio_dir: Path) -> list[Path]:
    """Generate every missing/short fixture wav. Returns the file list."""
    written: list[Path] = []
    for track in FIXTURE_TRACKS:
        path = audio_dir / track.filename
        expected = round(track.seconds * SAMPLE_RATE_HZ)
        if path.is_file() and _wav_frame_count(path) == expected:
            written.append(path)
            continue
        actual = write_tone_and_pulse_wav(path, track)
        if actual != expected:
            raise SystemExit(
                f"[ERROR] {path} wrote {actual} frames, expected {expected}"
            )
        written.append(path)
    for path in written:
        if _wav_frame_count(path) <= 0:
            raise SystemExit(f"[ERROR] {path} has no PCM frames")
    return written


# ----- real ingest -----------------------------------------------------------
def _state_cli(data_dir: Path, *args: str) -> None:
    """Run the REAL shared-state CLI against this fixture data dir."""
    env = dict(os.environ)
    env["MDT_DATA_DIR"] = str(data_dir)
    # The engine refuses WEB_CONCURRENCY; keep the builder's env identical to
    # the one the engine will boot under so a surprise cannot hide here.
    env.pop("WEB_CONCURRENCY", None)
    command = [
        "uv",
        "run",
        "--no-sync",
        "python",
        "-m",
        "apps.shared.state.cli",
        *args,
    ]
    result = subprocess.run(
        command, cwd=REPOSITORY_ROOT, env=env, capture_output=True, text=True,
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(
            f"[ERROR] {' '.join(args)} failed with exit code {result.returncode}"
        )
    sys.stdout.write(result.stdout)


def _track_rows(state_db: Path) -> list[tuple[str, str | None, str | None]]:
    conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        return [
            (str(sid), title, file_path)
            for sid, title, file_path in conn.execute(
                "SELECT stable_id, title, file_path FROM tracks ORDER BY title"
            )
        ]
    finally:
        conn.close()


def _discard_stale_revision(data_dir: Path) -> None:
    """Wipe a fixture dir built by an older revision.

    Reusing it would measure audio that no longer matches what the spec
    asserts, and the failure would look like a product bug rather than a
    stale fixture, so this is a wipe and not a warning.
    """
    marker = data_dir / REVISION_MARKER
    current = marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""
    if current == str(FIXTURE_REVISION):
        return
    for stale in (data_dir / AUDIO_SUBDIR, data_dir / "state"):
        if stale.exists():
            shutil.rmtree(stale)
    data_dir.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{FIXTURE_REVISION}\n", encoding="utf-8")


def _seed_playlists(
    state_db_path: Path, rows: list[tuple[str, str | None, str | None]]
) -> None:
    """Write browser-addressable populated and empty playlists through StateWriter."""
    stable_ids = [stable_id for stable_id, _title, _file_path in rows]
    if len(stable_ids) < 2:
        raise SystemExit("[ERROR] playlist fixture needs at least two ingested tracks")
    conn = state_db.open_rw(state_db_path)
    writer = StateWriter(conn, actor="e2e-deckload-fixture")
    try:
        writer.insert_playlist(
            playlist_id=POPULATED_PLAYLIST_ID,
            name="E2E Fixture Set",
            vendor="fixture",
            vendor_pl_id=POPULATED_PLAYLIST_ID,
        )
        writer.set_playlist_memberships(POPULATED_PLAYLIST_ID, stable_ids)
        writer.insert_playlist(
            playlist_id=EMPTY_PLAYLIST_ID,
            name="E2E Empty Set",
            vendor="fixture",
            vendor_pl_id=EMPTY_PLAYLIST_ID,
        )
        writer.set_playlist_memberships(EMPTY_PLAYLIST_ID, [])
    finally:
        writer.close()
        conn.close()


def build(
    data_dir: Path, *, seed_playlists: bool = False
) -> list[tuple[str, str | None, str | None]]:
    """Generate the audio, run the real ingest, verify the result."""
    _discard_stale_revision(data_dir)
    audio_dir = data_dir / AUDIO_SUBDIR
    files = ensure_audio(audio_dir)
    state_db = data_dir / "state" / "state.db"
    rows = _track_rows(state_db) if state_db.is_file() else []
    if len(rows) != len(files):
        _state_cli(data_dir, "init")
        _state_cli(
            data_dir, "ingest-folder", "--root", str(audio_dir), "--write"
        )
        rows = _track_rows(state_db)
    if len(rows) != len(files):
        raise SystemExit(
            f"[ERROR] fixture ingest wrote {len(rows)} track(s) for "
            f"{len(files)} audio file(s) in {audio_dir}"
        )
    for stable_id, _title, file_path in rows:
        if not file_path or not Path(file_path).is_file():
            raise SystemExit(
                f"[ERROR] track {stable_id} has no readable file_path: {file_path!r}"
            )
    if seed_playlists:
        _seed_playlists(state_db, rows)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="deckload_fixture",
        description="Build the webkit performance-suite fixture library.",
    )
    parser.add_argument(
        "--data-dir",
        required=True,
        help="absolute path of the throwaway fixture data dir",
    )
    parser.add_argument(
        "--seed-playlists",
        action="store_true",
        help="seed browser-addressable populated and empty playlists",
    )
    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir).expanduser()
    if not data_dir.is_absolute():
        raise SystemExit(f"[ERROR] --data-dir must be absolute, got {args.data_dir!r}")
    data_dir.mkdir(parents=True, exist_ok=True)
    rows = build(data_dir, seed_playlists=args.seed_playlists)
    print(f"[OK] fixture library at {data_dir} with {len(rows)} track(s):")
    for stable_id, title, file_path in rows:
        print(f"  {stable_id}  {title!r}  {file_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
