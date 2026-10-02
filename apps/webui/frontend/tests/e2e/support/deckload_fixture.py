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
peaks come from our own ffmpeg decode of the fixture audio (PARITY-08), so a
lane drawn here describes the real tone. The performance rescue path
(``--seed-rescue-playback``) runs the production ``apps.analysis`` librosa
backend over the generated accented audio and serves measured downbeats through
``/beatgrid-fallback``; it never writes a synthetic grid.

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

``feedback/`` is different: it holds RUN-scoped mutations the server writes in
response to real `feedback_mark`/pin/archive commands the specs dispatch, not
cached content keyed by revision, so every call here wipes it unconditionally
rather than gating it on `FIXTURE_REVISION`. Every call happens at config
import time in the Playwright MAIN process, before webServer boots the engine
and before any spec's test body runs, so this can never discard a mutation a
still-running test wrote. Workers also import the config, but only after the
engine is live on this data dir, so a config must not call this builder from a
worker (`TEST_WORKER_INDEX` set): a write here beside the live engine's own
commits fails with "database is locked" (job 109106840542).
Left alone, an interrupted `performance-feedback-card-dismiss.spec.ts` run
(killed after it seeds a pin, before its `finally` archives it) would leave
that pin behind forever, and the next run's `button.fb-pin[title^=...]`
locator would match it plus the new one, breaking Playwright's strict-mode
single-element assumption (Codex P2 BLOCKING, #1628).

Usage::

    uv run --no-sync python -m apps.webui.frontend.tests.e2e.support.deckload_fixture \
        --data-dir /abs/path/to/tests/e2e/fixtures/deckload-data --seed-playlists

``--manifest <abs path>`` writes a machine-readable JSON manifest (revision +
one row per track) alongside the human-readable stdout `main()` already
printed. Purely additive: no existing caller passes it, so nothing about the
5 other e2e gates sharing this builder changes shape.

``--seed-autoplay-chain`` is a SEPARATE opt-in, used only by the performance
suite. It layers a 3rd real track onto the base 2-track library (own
function, own playlist) and tags all three tracks with the SAME Camelot key
and BPM through the real ``StateWriter.set_field`` path (source="manual",
exactly how a manual tag edit lands in production) so AutoPlay's real
key/BPM compatibility gate has real, non-null data to walk instead of the
folder-ingest rows (which the module docstring above already documents as
carrying no bpm/key/beatgrid). It is deliberately its own function rather
than a change to ``build()``: the 2-track shape ``build()`` produces is
shared with playwright.webkit-deckload/preflight-gate/boot-burst/
hotcue-mapping-gate/comment-hotkey-gate configs, none of which pass this
flag, so their fixture is byte-for-byte unchanged.

``--seed-autoplay-hunt`` is another SEPARATE opt-in, used only by the
AutoPlay/mixing error hunt (#1853). It builds a 6-track library (own
function, own two playlists, own filenames) and is mutually exclusive with
``--seed-autoplay-chain`` so a caller cannot accidentally enlarge the
performance suite. Hunt files are new; ``FIXTURE_TRACKS`` and the chain
audio are unchanged, and ``FIXTURE_REVISION`` is not bumped.

Acceptance tests:

- [if] the ingest writes zero tracks [then] the builder exits non-zero.
- [if] a generated wav is header-only (no PCM frames) [then] it exits non-zero.
- [if] --data-dir is relative [then] it exits non-zero before writing anything.
- [if] the dir was built by an older FIXTURE_REVISION [then] the audio and the
  state db are wiped and rebuilt, never reused.
- [if] --seed-playlists is set [then] one populated and one empty playlist are
  written through StateWriter, never direct SQLite inserts.
- [if] --seed-autoplay-chain is set [then] a 3rd track is ingested and all
  three tracks carry the same real, non-null Camelot key, while each keeps its
  own declared BPM (128 / 124 / 128) so the distinct-BPM pair survives.
- [if] --seed-autoplay-hunt is set [then] 6 tracks are ingested into two
  playlists (A in declared order, B the reverse), all carrying Camelot key 8A
  and alternating 128 / 124 BPM, and ``FIXTURE_TRACKS`` stays a 2-track tuple.
- [if] --manifest is set [then] the written JSON's revision and track rows
  match what was actually ingested, read back from the manifest file itself.
- [if] ``feedback/`` holds a stale file from a prior (or interrupted) run
  [then] the next build call removes it, matching FIXTURE_REVISION or not.
"""

from __future__ import annotations

import argparse
import array
import hashlib
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import wave
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from apps.analysis.record import AnalysisRecord
from apps.analysis.store import fetch_records
from apps.shared.state import db as state_db
from apps.shared.state.writer import StateWriter

REPOSITORY_ROOT: Path = Path(__file__).resolve().parents[6]

#: Bump to invalidate every generated fixture dir. Anything that changes what
#: the audio SOUNDS like must bump this, or a stale dir keeps being measured.
FIXTURE_REVISION: int = 8
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
#: First beat of each four-beat bar is louder so librosa can establish bar phase.
BAR_DOWNBEAT_PULSE_MULTIPLIER: float = 5.0
BAR_ORDINARY_PULSE_MULTIPLIER: float = 0.55

#: The band the suite searches for the dominant bin. Wide enough to hold the
#: tone under any range this suite drives (-1 semitone to +16% is 1888..2320 Hz),
#: narrow enough to exclude the pulse fundamental and its low harmonics.
MEASUREMENT_BAND_HZ: tuple[float, float] = (800.0, 8_000.0)


#: ``pattern`` for a track whose bar is a kick on beats 1 and 3 and a noise
#: snare on 2 and 4. The plain accented pulse is gridded by the own beatgrid
#: producer (Beat This!) at 128 and 124 BPM, but at 64 BPM it reads every pulse
#: as a downbeat and the lane fails with ``grid_fit_bar_phase_below_floor``
#: (measured Thu 1 Oct 2026). A backbeat gives it a bar to find.
PATTERN_ACCENT: str = "accent"
PATTERN_BACKBEAT: str = "backbeat"
KICK_MS: float = 120.0
SNARE_MS: float = 80.0
SNARE_PEAK: float = 0.35


@dataclass(frozen=True)
class FixtureTrack:
    """One generated file. ``bpm`` is a property of the audio, not metadata."""

    filename: str
    bpm: float
    seconds: float
    pattern: str = PATTERN_ACCENT


#: Two tracks, because the suite loads deck 1 AND deck 2 and a single shared
#: file would hide any per-deck state bleed. Both are long enough that a
#: transport test can play, pause and seek without hitting the end.
FIXTURE_TRACKS: tuple[FixtureTrack, ...] = (
    FixtureTrack(filename="webkit-fixture-a-128bpm.wav", bpm=128.0, seconds=60.0),
    FixtureTrack(filename="webkit-fixture-b-124bpm.wav", bpm=124.0, seconds=60.0),
)

AUDIO_SUBDIR: str = "fixture-audio"

#: Where `apps/webui/server/routes/feedback.py::_dir` writes real feedback_mark
#: pins/comments/archives. Run-scoped mutation state, not cached content: unlike
#: AUDIO_SUBDIR and "state/", it is reset on every build call regardless of
#: FIXTURE_REVISION (see `_reset_feedback_dir`).
FEEDBACK_SUBDIR: str = "feedback"

POPULATED_PLAYLIST_ID: str = "e2e-fixture-populated"
EMPTY_PLAYLIST_ID: str = "e2e-fixture-empty"

#: --seed-autoplay-chain only. A 3rd track, same bpm/key family as track A
#: below, so a real (non-null) AutoPlay compatibility walk has >1 candidate
#: to chart. Kept out of FIXTURE_TRACKS so the 5 other e2e gates sharing
#: this builder never see a 3-track library.
AUTOPLAY_CHAIN_TRACK: FixtureTrack = FixtureTrack(
    filename="webkit-fixture-c-128bpm-chain.wav", bpm=128.0, seconds=60.0
)
#: Same Camelot key/BPM on every chain track (distance 0, ratio 1.0) so the
#: walk is compatible under any pitch-range tolerance, not just a generous one.
AUTOPLAY_CHAIN_CAMELOT_KEY: str = "8A"
AUTOPLAY_CHAIN_PLAYLIST_ID: str = "e2e-fixture-autoplay-chain"
AUTOPLAY_CHAIN_PLAYLIST_NAME: str = "E2E AutoPlay Chain"

#: --seed-rescue-playback only. A 64 BPM fold pair for the 128 BPM master so
#: BAR Beat Sync can exercise a real half-tempo lock in the hermetic lane.
#: 61 s, not 60: at 64 BPM a 60.0 s file ends exactly on beat 64, and the own
#: producer's +15 ms grid offset pushes that last beat past the record's
#: duration, so the lane contract refuses the whole record (measured Thu 1 Oct
#: 2026: "beatgrid.beats[64].t is 60.02656, beyond the record's duration_s
#: 60.02"). That is a producer defect reported separately, not hidden here.
PERFORMANCE_FOLD_TRACK: FixtureTrack = FixtureTrack(
    filename="webkit-fixture-d-64bpm-fold.wav", bpm=64.0, seconds=61.0,
    pattern=PATTERN_BACKBEAT,
)
RESCUE_PLAYBACK_TRACKS: tuple[FixtureTrack, ...] = (
    *FIXTURE_TRACKS,
    AUTOPLAY_CHAIN_TRACK,
    PERFORMANCE_FOLD_TRACK,
)
#: Four extra real ingested files for LIBUX-18 scroll/anchor e2e. No librosa
#: analysis and not added to the autoplay-chain playlist; each row maps to
#: its own wav through folder ingest.
RESCUE_PLAYBACK_BROWSE_TRACKS: tuple[FixtureTrack, ...] = (
    FixtureTrack(filename="webkit-fixture-e-120bpm-scroll.wav", bpm=120.0, seconds=60.0),
    FixtureTrack(filename="webkit-fixture-f-122bpm-scroll.wav", bpm=122.0, seconds=60.0),
    FixtureTrack(filename="webkit-fixture-g-126bpm-scroll.wav", bpm=126.0, seconds=60.0),
    FixtureTrack(filename="webkit-fixture-h-118bpm-scroll.wav", bpm=118.0, seconds=60.0),
)
#: Eight real rows: four analysis-backed transport tracks plus four browse-only.
RESCUE_PLAYBACK_LIBRARY_TRACKS: tuple[FixtureTrack, ...] = (
    *RESCUE_PLAYBACK_TRACKS,
    *RESCUE_PLAYBACK_BROWSE_TRACKS,
)
#: One browse-only rescue WAV carries embedded front-cover art (icon-192.png) so
#: performance row-chrome e2e can exercise the production /artwork route.
RESCUE_PLAYBACK_ARTWORK_TRACK: FixtureTrack = RESCUE_PLAYBACK_BROWSE_TRACKS[0]
ARTWORK_SOURCE_PNG: Path = REPOSITORY_ROOT / "apps/webui/frontend/static/icon-192.png"
ARTWORK_PNG_SHA256: str = (
    "c402d6f75569ba9627423e39fb1b8d41cd24019bc14671824564b2234f006ae2"
)

#: --seed-autoplay-hunt only. Six tracks, own filenames, kept out of
#: FIXTURE_TRACKS so the 5 other e2e gates sharing this builder never see a
#: 6-track library. Alternate 128 / 124 BPM so tempo/sync have a ratio
#: (124 vs 128 is 1.032, inside phase-lock). Same Camelot key as the chain
#: so AutoPlay compatibility is distance 0.
HUNT_CAMELOT_KEY: str = "8A"
HUNT_PLAYLIST_A_ID: str = "e2e-fixture-autoplay-hunt-a"
HUNT_PLAYLIST_A_NAME: str = "E2E AutoPlay Hunt A"
HUNT_PLAYLIST_B_ID: str = "e2e-fixture-autoplay-hunt-b"
HUNT_PLAYLIST_B_NAME: str = "E2E AutoPlay Hunt B"
HUNT_TRACKS: tuple[FixtureTrack, ...] = (
    FixtureTrack(filename="webkit-fixture-hunt-01-128bpm.wav", bpm=128.0, seconds=60.0),
    FixtureTrack(filename="webkit-fixture-hunt-02-124bpm.wav", bpm=124.0, seconds=60.0),
    FixtureTrack(filename="webkit-fixture-hunt-03-128bpm.wav", bpm=128.0, seconds=60.0),
    FixtureTrack(filename="webkit-fixture-hunt-04-124bpm.wav", bpm=124.0, seconds=60.0),
    FixtureTrack(filename="webkit-fixture-hunt-05-128bpm.wav", bpm=128.0, seconds=60.0),
    FixtureTrack(filename="webkit-fixture-hunt-06-124bpm.wav", bpm=124.0, seconds=60.0),
)

MANIFEST_FILENAME: str = "fixture-manifest.json"


# ----- audio generation ------------------------------------------------------
def _pulse_envelope(sample_index: int, beat_period_samples: float) -> float:
    """Linear-decay envelope for the transient that starts on each beat."""
    pulse_len = SAMPLE_RATE_HZ * PULSE_MS / 1000.0
    since_beat = sample_index % beat_period_samples
    if since_beat >= pulse_len:
        return 0.0
    return 1.0 - (since_beat / pulse_len)


def _noise(sample_index: int) -> float:
    """Deterministic white noise in [-1, 1): the same bytes on every host."""
    # splitmix32-style integer hash of the index (a plain LCG of the index is
    # linear in it, which is a sawtooth, not noise).
    state = (sample_index + 0x9E37_79B9) & 0xFFFF_FFFF
    state = ((state ^ (state >> 16)) * 0x85EB_CA6B) & 0xFFFF_FFFF
    state = ((state ^ (state >> 13)) * 0xC2B2_AE35) & 0xFFFF_FFFF
    state ^= state >> 16
    return state / float(0x8000_0000) - 1.0


def _backbeat_value(sample_index: int, beat_period_samples: float) -> float:
    """Kick on beats 1 and 3 (beat 1 louder), noise snare on 2 and 4."""
    beat_index = int(sample_index / beat_period_samples)
    since_beat = sample_index - beat_index * beat_period_samples
    position = beat_index % 4
    if position in (0, 2):
        length = SAMPLE_RATE_HZ * KICK_MS / 1000.0
        if since_beat >= length:
            return 0.0
        env = 1.0 - since_beat / length
        sweep_hz = PULSE_HZ * 0.6 * (1.0 + env)
        peak = 0.9 if position == 0 else 0.6
        return peak * env * math.sin(2.0 * math.pi * sweep_hz * since_beat / SAMPLE_RATE_HZ)
    length = SAMPLE_RATE_HZ * SNARE_MS / 1000.0
    if since_beat >= length:
        return 0.0
    return SNARE_PEAK * (1.0 - since_beat / length) * _noise(sample_index)


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
    samples = array.array("h")
    for n in range(frame_count):
        value = TONE_PEAK * math.sin(tone_step * n)
        if track.pattern == PATTERN_BACKBEAT:
            value += _backbeat_value(n, beat_period_samples)
            sample = int(max(-1.0, min(1.0, value)) * peak)
            samples.append(sample)
            samples.append(sample)
            continue
        env = _pulse_envelope(n, beat_period_samples)
        if env > 0.0:
            beat_index = int(n / beat_period_samples)
            if beat_index % 4 == 0:
                pulse_peak = PULSE_PEAK * BAR_DOWNBEAT_PULSE_MULTIPLIER
                pulse_hz = PULSE_HZ * 1.8
            else:
                pulse_peak = PULSE_PEAK * BAR_ORDINARY_PULSE_MULTIPLIER
                pulse_hz = PULSE_HZ
            pulse_step = 2.0 * math.pi * pulse_hz / SAMPLE_RATE_HZ
            value += pulse_peak * env * math.sin(pulse_step * n)
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


def ensure_audio(
    audio_dir: Path, tracks: tuple[FixtureTrack, ...] = FIXTURE_TRACKS
) -> list[Path]:
    """Generate every missing/short fixture wav. Returns the file list.

    ``tracks`` defaults to the 2-track library the five other e2e configs
    share; ``build_autoplay_chain`` passes its own longer tuple so both
    callers ensure audio through exactly this one code path.
    """
    written: list[Path] = []
    for track in tracks:
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
def _ingest_and_verify(
    data_dir: Path, audio_dir: Path, files: list[Path], label: str
) -> list[tuple[str, str | None, str | None]]:
    """Run the REAL ingest until the DB holds one row per audio file.

    Shared by ``build`` and ``build_autoplay_chain`` so the "N rows for N
    files" invariant and the readable-``file_path`` check have exactly one
    implementation. ``label`` only names the caller in the failure message.
    """
    state_db_path = data_dir / "state" / "state.db"
    rows = _track_rows(state_db_path) if state_db_path.is_file() else []
    if len(rows) != len(files):
        _state_cli(data_dir, "init")
        _state_cli(data_dir, "ingest-folder", "--root", str(audio_dir), "--write")
        rows = _track_rows(state_db_path)
    if len(rows) != len(files):
        raise SystemExit(
            f"[ERROR] {label} ingest wrote {len(rows)} track(s) for "
            f"{len(files)} audio file(s) in {audio_dir}"
        )
    for stable_id, _title, file_path in rows:
        if not file_path or not Path(file_path).is_file():
            raise SystemExit(
                f"[ERROR] track {stable_id} has no readable file_path: {file_path!r}"
            )
    return rows


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


def _reset_feedback_dir(data_dir: Path) -> None:
    """Wipe any feedback state left by a prior or interrupted run.

    Unlike ``AUDIO_SUBDIR``/"state", this is not cached content gated on
    ``FIXTURE_REVISION``: it is mutation state the SERVER writes when a spec
    dispatches a real feedback_mark/pin/archive command, so it is reset on
    EVERY call regardless of revision. A run killed after
    `performance-feedback-card-dismiss.spec.ts` seeds a pin but before its
    `finally` archives it would otherwise leave that pin in place forever,
    and the next run's `button.fb-pin[title^=...]` locator would then match
    both the stale pin and the new one, breaking Playwright's strict-mode
    single-element assumption (Codex P2 BLOCKING, #1628).
    """
    feedback_dir = data_dir / FEEDBACK_SUBDIR
    if feedback_dir.exists():
        shutil.rmtree(feedback_dir)


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


def build_autoplay_chain(
    data_dir: Path,
) -> list[tuple[str, str | None, str | None]]:
    """Extend ``build()``'s 2-track library with a 3rd, key/BPM-tagged track.

    Own function, own playlist (``AUTOPLAY_CHAIN_PLAYLIST_ID``): ``build()``
    itself, and the 2-track shape it produces, are untouched, so the 5 other
    e2e configs sharing this builder never see a 3rd track. Only the
    performance suite calls this.

    Tags land through the real ``StateWriter.set_field`` path with
    ``source="manual"`` -- the same call a manual tag edit makes in
    production (see ``apps/webui/server/sqlite_backend.py``'s PATCH
    /tracks/{stable_id} handler) -- never a direct SQLite write. Every track
    gets the SAME Camelot key and BPM, so AutoPlay's real compatibility gate
    (Camelot distance <= 1, BPM ratio inside phase-lock range) is satisfied
    at distance 0 / ratio 1.0 regardless of whatever pitch-range tolerance
    the page has active.
    """
    # Deliberately NOT `build(data_dir, seed_playlists=True)` followed by a
    # separate ingest for the chain file: once this has run once, the audio
    # dir holds 3 files while `build()`'s own `ensure_audio()` only ever
    # accounts for the 2-track `FIXTURE_TRACKS` tuple, so a second call to
    # plain `build()` on an already-chain-extended dir sees "3 rows for 2
    # files" and raises. Playwright re-imports this config more than once
    # per run (webServer startup + worker startup both evaluate it), so this
    # function must tolerate being called repeatedly against its OWN fully-
    # built output, not just against an empty dir. Folding FIXTURE_TRACKS and
    # the chain track into one list, ensured and ingested together, keeps
    # the "N rows for N files" invariant true no matter how many times this
    # runs against the same dir.
    _discard_stale_revision(data_dir)
    _reset_feedback_dir(data_dir)
    audio_dir = data_dir / AUDIO_SUBDIR
    generated_unsorted = (*FIXTURE_TRACKS, AUTOPLAY_CHAIN_TRACK)
    files = ensure_audio(audio_dir, generated_unsorted)
    state_db_path = data_dir / "state" / "state.db"
    rows = _ingest_and_verify(data_dir, audio_dir, files, "autoplay-chain")

    # Keeps the manifest's populated/empty playlist ids honest: `write_manifest`
    # always records them, so they must always actually exist in the DB, same
    # as the plain `build(..., seed_playlists=True)` path.
    _seed_playlists(state_db_path, rows)

    stable_ids = [stable_id for stable_id, _title, _file_path in rows]
    # `_track_rows` returns rows ORDER BY title, so line the generated tracks up
    # with them by filename rather than assuming the tuple order survived.
    by_filename = {track.filename: track for track in generated_unsorted}
    generated = [
        by_filename[Path(file_path).name]
        for _stable_id, _title, file_path in rows
        if file_path is not None and Path(file_path).name in by_filename
    ]
    now = datetime.now(UTC).isoformat()
    conn = state_db.open_rw(state_db_path)
    writer = StateWriter(conn, actor="e2e-deckload-fixture")
    # Every track gets the SAME Camelot key, so AutoPlay's compatibility gate is
    # satisfied at distance 0. Each track keeps its OWN declared BPM: 124 and 128
    # are 1.032 apart, comfortably inside phase-lock range for AutoPlay, and
    # flattening them all to one value would destroy the distinct-BPM pair
    # `performance-controls.spec.ts::_realSyncPair` needs (it requires a ratio
    # differing by >= 0.005, so an all-128 library makes it throw
    # deterministically). Bot review, Wed 9 Sep 2026.
    bpm_by_stable_id = {
        stable_id: track.bpm
        for (stable_id, _title, file_path), track in zip(rows, generated, strict=True)
        if file_path is not None and Path(file_path).name == track.filename
    }
    if len(bpm_by_stable_id) != len(rows):
        raise SystemExit(
            "[ERROR] autoplay chain could not match every ingested row to its generated file"
        )
    try:
        for stable_id in stable_ids:
            writer.set_field(
                stable_id, "bpm", bpm_by_stable_id[stable_id],
                source="manual", modified_at=now, confidence=1.0,
            )
            writer.set_field(
                stable_id, "key", AUTOPLAY_CHAIN_CAMELOT_KEY,
                source="manual", modified_at=now, confidence=1.0,
            )
        writer.insert_playlist(
            playlist_id=AUTOPLAY_CHAIN_PLAYLIST_ID,
            name=AUTOPLAY_CHAIN_PLAYLIST_NAME,
            vendor="fixture",
            vendor_pl_id=AUTOPLAY_CHAIN_PLAYLIST_ID,
        )
        writer.set_playlist_memberships(AUTOPLAY_CHAIN_PLAYLIST_ID, stable_ids)
    finally:
        writer.close()
        conn.close()
    return rows


def _stable_ids_in_track_order(
    rows: list[tuple[str, str | None, str | None]],
    tracks: tuple[FixtureTrack, ...],
    label: str,
) -> list[str]:
    """Line ingested rows up with ``tracks`` by filename, in tuple order."""
    by_filename: dict[str, str] = {}
    for stable_id, _title, file_path in rows:
        if file_path is None:
            continue
        by_filename[Path(file_path).name] = stable_id
    ordered: list[str] = []
    missing: list[str] = []
    for track in tracks:
        stable_id = by_filename.get(track.filename)
        if stable_id is None:
            missing.append(track.filename)
            continue
        ordered.append(stable_id)
    if missing or len(ordered) != len(tracks):
        raise SystemExit(
            f"[ERROR] {label} could not match every generated file to an ingested row "
            f"(missing {missing!r})"
        )
    return ordered


def _pairs_for_generated_tracks(
    rows: list[tuple[str, str | None, str | None]],
    tracks: tuple[FixtureTrack, ...],
    label: str,
) -> list[list[str]]:
    """Map ingested rows onto generated audio files, in a pairs-json shape."""
    by_filename = {track.filename: track for track in tracks}
    pairs: list[list[str]] = []
    for stable_id, _title, file_path in rows:
        if not file_path:
            raise SystemExit(f"[ERROR] {label} row {stable_id!r} has no file_path")
        audio_path = Path(file_path)
        if audio_path.name not in by_filename:
            continue
        pairs.append([stable_id, str(audio_path)])
    if len(pairs) != len(tracks):
        raise SystemExit(
            f"[ERROR] {label} could not map every generated track to an ingested row "
            f"(expected {len(tracks)}, got {len(pairs)})"
        )
    return pairs


def _assert_measured_downbeats(
    stored: list[dict[str, Any]],
    tracks: tuple[FixtureTrack, ...],
    label: str,
) -> None:
    """Fail closed when a fixture row is missing a measured bar phase."""
    if len(stored) != len(tracks):
        raise SystemExit(
            f"[ERROR] {label} wrote {len(stored)} librosa row(s) for {len(tracks)} track(s)"
        )
    for row in stored:
        record = AnalysisRecord.from_json(row["record_json"])
        if not record.downbeats_s:
            raise SystemExit(
                f"[ERROR] {label} track {record.stable_id} has no measured downbeats"
            )
        if record.downbeats_s != sorted(record.downbeats_s):
            raise SystemExit(
                f"[ERROR] {label} track {record.stable_id} downbeats are not increasing"
            )
        if record.duration_s and record.downbeats_s[-1] > record.duration_s:
            raise SystemExit(
                f"[ERROR] {label} track {record.stable_id} downbeats exceed duration"
            )
        if record.features_blob.get("downbeat_tracking") is not True:
            raise SystemExit(
                f"[ERROR] {label} track {record.stable_id} did not establish bar phase"
            )


def _sid_for_filename(
    rows: list[tuple[str, str | None, str | None]], filename: str
) -> str:
    return next(
        stable_id
        for stable_id, _title, file_path in rows
        if file_path is not None and Path(file_path).name == filename
    )


def _assert_fixture_bpms(
    rows: list[tuple[str, str | None, str | None]],
    stored: list[dict[str, Any]],
    tracks: tuple[FixtureTrack, ...],
    label: str,
) -> None:
    """The fold and master tracks must measure near their generated tempos."""
    by_filename = {track.filename: track for track in tracks}
    by_stable_id = {
        row["stable_id"]: AnalysisRecord.from_json(row["record_json"]) for row in stored
    }
    fold_track = by_filename[PERFORMANCE_FOLD_TRACK.filename]
    master_track = by_filename[FIXTURE_TRACKS[0].filename]
    fold_bpm = by_stable_id[_sid_for_filename(rows, PERFORMANCE_FOLD_TRACK.filename)].bpm
    master_bpm = by_stable_id[_sid_for_filename(rows, FIXTURE_TRACKS[0].filename)].bpm
    if abs(fold_bpm - fold_track.bpm) > 4.0:
        raise SystemExit(
            f"[ERROR] {label} fold track measured {fold_bpm:.1f} bpm, "
            f"expected near {fold_track.bpm}"
        )
    if abs(master_bpm - master_track.bpm) > 4.0:
        raise SystemExit(
            f"[ERROR] {label} master track measured {master_bpm:.1f} bpm, "
            f"expected near {master_track.bpm}"
        )


def _run_librosa_analysis(
    data_dir: Path,
    rows: list[tuple[str, str | None, str | None]],
    tracks: tuple[FixtureTrack, ...],
    label: str,
) -> None:
    """Run the production librosa analyzer and fail closed on missing downbeats."""
    pairs = _pairs_for_generated_tracks(rows, tracks, label)
    pairs_path = data_dir / "analysis-pairs.json"
    pairs_path.write_text(json.dumps(pairs, indent=2) + "\n", encoding="utf-8")
    env = dict(os.environ)
    env["MDT_DATA_DIR"] = str(data_dir)
    env.pop("WEB_CONCURRENCY", None)
    command = [
        "uv",
        "run",
        "--no-sync",
        "python",
        "-m",
        "apps.analysis.run",
        "--pairs-json",
        str(pairs_path),
        "--backend",
        "librosa",
    ]
    result = subprocess.run(
        command, cwd=REPOSITORY_ROOT, env=env, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(
            f"[ERROR] {label} librosa analysis failed with exit code {result.returncode}"
        )
    pairs_path.unlink(missing_ok=True)

    state_db_path = data_dir / "state" / "state.db"
    analysis_filenames = {track.filename for track in tracks}
    stable_ids = [
        stable_id
        for stable_id, _title, file_path in rows
        if file_path and Path(file_path).name in analysis_filenames
    ]
    stored = fetch_records(stable_ids=stable_ids, backend="librosa", db_path=state_db_path)
    _assert_measured_downbeats(stored, tracks, label)
    _assert_fixture_bpms(rows, stored, tracks, label)


_RESCUE_ARTWORK_EMBED_SCRIPT = """
import hashlib
import sys
from pathlib import Path

from apps.shared import audio_files
from mutagen.id3 import APIC
from mutagen.wave import WAVE

wav_path = Path(sys.argv[1])
png_path = Path(sys.argv[2])
expected_sha = sys.argv[3]
png_bytes = png_path.read_bytes()
if hashlib.sha256(png_bytes).hexdigest() != expected_sha:
    raise SystemExit("[ERROR] artwork PNG checksum mismatch in embed worker")
def _assert_single_front_cover_apic(tags) -> None:
    apics = tags.getall("APIC") or []
    if len(apics) != 1:
        raise SystemExit(f"[ERROR] expected exactly one APIC frame, got {len(apics)}")
    frame = apics[0]
    if frame.type != 3 or frame.mime != "image/png":
        raise SystemExit(
            f"[ERROR] expected front-cover PNG APIC, got type={frame.type} mime={frame.mime}"
        )


if audio_files.read_embedded_artwork(wav_path) == (png_bytes, "image/png"):
    audio = WAVE(wav_path)
    if audio.tags is None:
        raise SystemExit("[ERROR] artwork wav has bytes but no ID3 tags")
    _assert_single_front_cover_apic(audio.tags)
    raise SystemExit(0)
audio = WAVE(wav_path)
if audio.tags is None:
    audio.add_tags()
audio.tags.delall("APIC")
audio.tags.add(APIC(encoding=3, mime="image/png", type=3, desc="cover", data=png_bytes))
audio.save()
if audio_files.read_embedded_artwork(wav_path) != (png_bytes, "image/png"):
    raise SystemExit("[ERROR] embedded rescue artwork did not round-trip")
audio = WAVE(wav_path)
_assert_single_front_cover_apic(audio.tags)
"""


def _ensure_rescue_artwork_embedded(audio_dir: Path) -> None:
    """Embed the checked-in PNG on the designated browse-only rescue WAV (idempotent)."""
    wav_path = audio_dir / RESCUE_PLAYBACK_ARTWORK_TRACK.filename
    if not wav_path.is_file():
        raise SystemExit(f"[ERROR] rescue artwork wav missing: {wav_path}")
    png_bytes = ARTWORK_SOURCE_PNG.read_bytes()
    if png_bytes[:8] != b"\x89PNG\r\n\x1a\n":
        raise SystemExit(f"[ERROR] artwork source is not a PNG: {ARTWORK_SOURCE_PNG}")
    digest = hashlib.sha256(png_bytes).hexdigest()
    if digest != ARTWORK_PNG_SHA256:
        raise SystemExit(
            f"[ERROR] artwork PNG checksum mismatch for {ARTWORK_SOURCE_PNG}: {digest}"
        )
    command = [
        "uv",
        "run",
        "--no-sync",
        "--extra",
        "tags",
        "python",
        "-c",
        _RESCUE_ARTWORK_EMBED_SCRIPT,
        str(wav_path),
        str(ARTWORK_SOURCE_PNG),
        ARTWORK_PNG_SHA256,
    ]
    result = subprocess.run(
        command, cwd=REPOSITORY_ROOT, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(
            f"[ERROR] rescue artwork embed failed with exit code {result.returncode}"
        )


#: The own beatgrid producer's backend name (``apps.analysis.backends.own_beatgrid``).
OWN_BEATGRID_BACKEND: str = "own_beatgrid.backfill"
#: Where the producer finds its verified Beat This! checkpoint
#: (``apps.analysis_beatgrid.weights.WEIGHTS_PATH_ENV``).
OWN_BEATGRID_WEIGHTS_ENV: str = "MDT_BEATGRID_WEIGHTS"
#: A grid this far from the generated tempo is a wrong grid, not jitter.
OWN_BEATGRID_BPM_TOLERANCE: float = 0.5


def _run_own_beatgrid_analysis(
    data_dir: Path,
    rows: list[tuple[str, str | None, str | None]],
    tracks: tuple[FixtureTrack, ...],
    label: str,
) -> None:
    """Run the PRODUCTION own beatgrid producer and fail closed on any non-ok lane.

    Since #3561 the deck refuses the legacy librosa ``/beatgrid-fallback`` grid
    for a track whose own lane reports ``missing``, which is every unmapped
    fixture track, so without this step no fixture deck has a grid and Beat
    Sync cannot be exercised at all. This is the same backfill a real library
    gets (``apps.analysis.run --backend own_beatgrid.backfill``: Beat This! on
    the generated audio, grid fit, canonical pointer), so ``/anlz`` then serves
    a measured ``source: own, status: ok`` grid and the #3561 rule is untouched.
    Nothing is written by hand.
    """
    weights = os.environ.get(OWN_BEATGRID_WEIGHTS_ENV, "").strip()
    if not weights or not Path(weights).is_file():
        raise SystemExit(
            f"[ERROR] {label} --seed-own-beatgrid needs {OWN_BEATGRID_WEIGHTS_ENV} naming "
            f"the verified Beat This! checkpoint file, got {weights!r}"
        )
    pairs = _pairs_for_generated_tracks(rows, tracks, label)
    pairs_path = data_dir / "own-beatgrid-pairs.json"
    pairs_path.write_text(json.dumps(pairs, indent=2) + "\n", encoding="utf-8")
    env = dict(os.environ)
    env["MDT_DATA_DIR"] = str(data_dir)
    env.pop("WEB_CONCURRENCY", None)
    command = [
        "uv", "run", "--no-sync", "python", "-m", "apps.analysis.run",
        "--pairs-json", str(pairs_path), "--backend", OWN_BEATGRID_BACKEND,
    ]
    result = subprocess.run(
        command, cwd=REPOSITORY_ROOT, env=env, capture_output=True, text=True, check=False,
    )
    pairs_path.unlink(missing_ok=True)
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(
            f"[ERROR] {label} own beatgrid analysis failed with exit code {result.returncode}"
        )
    _assert_own_beatgrids(data_dir, rows, tracks, label)


def _assert_own_beatgrids(
    data_dir: Path,
    rows: list[tuple[str, str | None, str | None]],
    tracks: tuple[FixtureTrack, ...],
    label: str,
) -> None:
    """Every generated track has a canonical own grid, ok, at its generated tempo."""
    from apps.analysis.canonical import canonical_pointer

    by_filename = {track.filename: track for track in tracks}
    conn = sqlite3.connect(f"file:{data_dir / 'state' / 'state.db'}?mode=ro", uri=True)
    try:
        for stable_id, _title, file_path in rows:
            if file_path is None or Path(file_path).name not in by_filename:
                continue
            track = by_filename[Path(file_path).name]
            pointer = canonical_pointer(conn, stable_id, "beatgrid")
            if pointer is None:
                raise SystemExit(
                    f"[ERROR] {label} {track.filename} has no canonical own beatgrid"
                )
            row = conn.execute(
                "SELECT record_json FROM analysis "
                "WHERE stable_id = ? AND backend = ? AND backend_version = ?",
                (stable_id, pointer[0], pointer[1]),
            ).fetchone()
            lane = AnalysisRecord.from_json(row[0]).lanes.get("beatgrid")
            if lane is None or lane.status != "ok":
                status = None if lane is None else (lane.status, lane.reason)
                raise SystemExit(
                    f"[ERROR] {label} {track.filename} own beatgrid is not ok: {status}"
                )
            measured = float(lane.payload["bpm"])
            if abs(measured - track.bpm) > OWN_BEATGRID_BPM_TOLERANCE:
                raise SystemExit(
                    f"[ERROR] {label} {track.filename} own beatgrid measured "
                    f"{measured:.2f} bpm, generated at {track.bpm}"
                )
            if not any(int(beat["n"]) == 1 for beat in lane.payload["beats"]):
                raise SystemExit(
                    f"[ERROR] {label} {track.filename} own beatgrid has no beat-1 marker"
                )
    finally:
        conn.close()


def build_rescue_playback(
    data_dir: Path, *, own_beatgrid: bool = False
) -> list[tuple[str, str | None, str | None]]:
    """Build an analysis-backed performance rescue library for Beat Sync e2e.

    ``own_beatgrid`` additionally runs the production own beatgrid producer
    (see :func:`_run_own_beatgrid_analysis`), which is what gives the decks a
    grid at all since #3561.
    """
    _discard_stale_revision(data_dir)
    _reset_feedback_dir(data_dir)
    audio_dir = data_dir / AUDIO_SUBDIR
    files = ensure_audio(audio_dir, RESCUE_PLAYBACK_LIBRARY_TRACKS)
    _ensure_rescue_artwork_embedded(audio_dir)
    state_db_path = data_dir / "state" / "state.db"
    rows = _ingest_and_verify(data_dir, audio_dir, files, "rescue-playback")
    _seed_playlists(state_db_path, rows)

    transport_filenames = {track.filename for track in RESCUE_PLAYBACK_TRACKS}
    autoplay_stable_ids = [
        stable_id
        for stable_id, _title, file_path in rows
        if file_path and Path(file_path).name in transport_filenames
    ]
    if len(autoplay_stable_ids) != len(RESCUE_PLAYBACK_TRACKS):
        raise SystemExit(
            f"[ERROR] rescue-playback expected {len(RESCUE_PLAYBACK_TRACKS)} autoplay-chain "
            f"members, got {len(autoplay_stable_ids)}"
        )
    by_filename = {track.filename: track for track in RESCUE_PLAYBACK_LIBRARY_TRACKS}
    now = datetime.now(UTC).isoformat()
    conn = state_db.open_rw(state_db_path)
    writer = StateWriter(conn, actor="e2e-deckload-fixture")
    bpm_by_stable_id: dict[str, float] = {}
    for stable_id, _title, file_path in rows:
        if file_path is None:
            raise SystemExit(f"[ERROR] rescue-playback row {stable_id!r} has no file_path")
        track = by_filename.get(Path(file_path).name)
        if track is None:
            raise SystemExit(
                f"[ERROR] rescue-playback ingested unknown file {Path(file_path).name!r}"
            )
        bpm_by_stable_id[stable_id] = track.bpm
    if len(bpm_by_stable_id) != len(rows):
        raise SystemExit(
            "[ERROR] rescue-playback could not match every ingested row to its generated file"
        )
    try:
        for stable_id, bpm in bpm_by_stable_id.items():
            writer.set_field(
                stable_id, "bpm", bpm,
                source="manual", modified_at=now, confidence=1.0,
            )
            writer.set_field(
                stable_id, "key", AUTOPLAY_CHAIN_CAMELOT_KEY,
                source="manual", modified_at=now, confidence=1.0,
            )
        writer.insert_playlist(
            playlist_id=AUTOPLAY_CHAIN_PLAYLIST_ID,
            name=AUTOPLAY_CHAIN_PLAYLIST_NAME,
            vendor="fixture",
            vendor_pl_id=AUTOPLAY_CHAIN_PLAYLIST_ID,
        )
        writer.set_playlist_memberships(AUTOPLAY_CHAIN_PLAYLIST_ID, autoplay_stable_ids)
    finally:
        writer.close()
        conn.close()

    _run_librosa_analysis(data_dir, rows, RESCUE_PLAYBACK_TRACKS, "rescue-playback")
    if own_beatgrid:
        _run_own_beatgrid_analysis(data_dir, rows, RESCUE_PLAYBACK_TRACKS, "rescue-playback")
    return rows


def build_autoplay_hunt(
    data_dir: Path,
) -> list[tuple[str, str | None, str | None]]:
    """Build a 6-track hunt library with two playlists. Does not call ``build()``.

    Own function, own playlist ids: ``build()`` itself, and the 2-track shape
    it produces, are untouched. Deliberately NOT ``build()`` followed by a
    second ingest -- once this has run once, the audio dir holds 6 files
    while ``build()``'s ``ensure_audio()`` only accounts for ``FIXTURE_TRACKS``,
    so a later plain ``build()`` on this dir would see "6 rows for 2 files".
    Playwright re-imports its config more than once per run, so this function
    must tolerate being called repeatedly against its own fully-built output.

    Playlist A is the 6 tracks in declared (HUNT_TRACKS) order; playlist B
    is the reverse membership of the same 6. Tags land through
    ``StateWriter.set_field`` with ``source="manual"``.
    """
    if len(HUNT_TRACKS) < 6:
        raise SystemExit("[ERROR] HUNT_TRACKS must contain at least 6 tracks")
    _discard_stale_revision(data_dir)
    _reset_feedback_dir(data_dir)
    audio_dir = data_dir / AUDIO_SUBDIR
    files = ensure_audio(audio_dir, HUNT_TRACKS)
    state_db_path = data_dir / "state" / "state.db"
    rows = _ingest_and_verify(data_dir, audio_dir, files, "autoplay-hunt")
    _seed_playlists(state_db_path, rows)

    ordered_ids = _stable_ids_in_track_order(rows, HUNT_TRACKS, "autoplay-hunt")
    bpm_by_stable_id = {
        stable_id: track.bpm for stable_id, track in zip(ordered_ids, HUNT_TRACKS, strict=True)
    }
    now = datetime.now(UTC).isoformat()
    conn = state_db.open_rw(state_db_path)
    writer = StateWriter(conn, actor="e2e-deckload-fixture")
    try:
        for stable_id in ordered_ids:
            writer.set_field(
                stable_id, "bpm", bpm_by_stable_id[stable_id],
                source="manual", modified_at=now, confidence=1.0,
            )
            writer.set_field(
                stable_id, "key", HUNT_CAMELOT_KEY,
                source="manual", modified_at=now, confidence=1.0,
            )
        writer.insert_playlist(
            playlist_id=HUNT_PLAYLIST_A_ID,
            name=HUNT_PLAYLIST_A_NAME,
            vendor="fixture",
            vendor_pl_id=HUNT_PLAYLIST_A_ID,
        )
        writer.set_playlist_memberships(HUNT_PLAYLIST_A_ID, ordered_ids)
        writer.insert_playlist(
            playlist_id=HUNT_PLAYLIST_B_ID,
            name=HUNT_PLAYLIST_B_NAME,
            vendor="fixture",
            vendor_pl_id=HUNT_PLAYLIST_B_ID,
        )
        writer.set_playlist_memberships(HUNT_PLAYLIST_B_ID, list(reversed(ordered_ids)))
    finally:
        writer.close()
        conn.close()
    return rows


def write_manifest(
    manifest_path: Path,
    rows: list[tuple[str, str | None, str | None]],
    *,
    autoplay_chain_playlist_id: str | None = None,
    autoplay_chain_playlist_name: str | None = None,
    autoplay_hunt_playlist_a_id: str | None = None,
    autoplay_hunt_playlist_a_name: str | None = None,
    autoplay_hunt_playlist_b_id: str | None = None,
    autoplay_hunt_playlist_b_name: str | None = None,
) -> None:
    """Write the machine-readable sibling of ``main()``'s stdout listing.

    Consumed by ``tests/e2e/support/fixture-manifest.ts``. Fails loudly
    rather than writing a partial file: an empty ``rows`` here would make a
    reader believe the fixture built successfully with zero tracks.
    """
    if not rows:
        raise SystemExit("[ERROR] refusing to write a manifest with zero tracks")
    payload = {
        "fixture_revision": FIXTURE_REVISION,
        "populated_playlist_id": POPULATED_PLAYLIST_ID,
        "empty_playlist_id": EMPTY_PLAYLIST_ID,
        "autoplay_chain_playlist_id": autoplay_chain_playlist_id,
        "autoplay_chain_playlist_name": autoplay_chain_playlist_name,
        "autoplay_hunt_playlist_a_id": autoplay_hunt_playlist_a_id,
        "autoplay_hunt_playlist_a_name": autoplay_hunt_playlist_a_name,
        "autoplay_hunt_playlist_b_id": autoplay_hunt_playlist_b_id,
        "autoplay_hunt_playlist_b_name": autoplay_hunt_playlist_b_name,
        "tracks": [
            {"stable_id": stable_id, "title": title, "file_path": file_path}
            for stable_id, title, file_path in rows
        ],
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def build(
    data_dir: Path, *, seed_playlists: bool = False
) -> list[tuple[str, str | None, str | None]]:
    """Generate the audio, run the real ingest, verify the result."""
    _discard_stale_revision(data_dir)
    _reset_feedback_dir(data_dir)
    audio_dir = data_dir / AUDIO_SUBDIR
    files = ensure_audio(audio_dir)
    rows = _ingest_and_verify(data_dir, audio_dir, files, "fixture")
    if seed_playlists:
        _seed_playlists(data_dir / "state" / "state.db", rows)
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
    parser.add_argument(
        "--seed-autoplay-chain",
        action="store_true",
        help=(
            "add a 3rd, key/BPM-tagged track and its own AutoPlay-chain "
            "playlist (performance suite only; implies --seed-playlists)"
        ),
    )
    parser.add_argument(
        "--seed-autoplay-hunt",
        action="store_true",
        help=(
            "build a 6-track hunt library with two playlists (error-hunt "
            "suite only; implies --seed-playlists; exclusive with "
            "--seed-autoplay-chain)"
        ),
    )
    parser.add_argument(
        "--seed-rescue-playback",
        action="store_true",
        help=(
            "autoplay-chain library plus a 64 bpm fold track, analyzed through "
            "the production librosa backend for rescue playback e2e "
            "(performance suite only)"
        ),
    )
    parser.add_argument(
        "--seed-own-beatgrid",
        action="store_true",
        help=(
            "with --seed-rescue-playback: also run the production own beatgrid "
            "producer (Beat This!), so /anlz serves a measured own grid; needs "
            f"{OWN_BEATGRID_WEIGHTS_ENV} and ffmpeg"
        ),
    )
    parser.add_argument(
        "--manifest",
        default=None,
        help="absolute path to write a machine-readable JSON manifest",
    )
    args = parser.parse_args(argv)
    if sum(
        int(flag)
        for flag in (args.seed_autoplay_chain, args.seed_autoplay_hunt, args.seed_rescue_playback)
    ) > 1:
        raise SystemExit(
            "[ERROR] --seed-autoplay-chain, --seed-autoplay-hunt, and "
            "--seed-rescue-playback are mutually exclusive"
        )
    if args.seed_own_beatgrid and not args.seed_rescue_playback:
        raise SystemExit("[ERROR] --seed-own-beatgrid requires --seed-rescue-playback")
    data_dir = Path(args.data_dir).expanduser()
    if not data_dir.is_absolute():
        raise SystemExit(f"[ERROR] --data-dir must be absolute, got {args.data_dir!r}")
    data_dir.mkdir(parents=True, exist_ok=True)
    autoplay_chain_playlist_id: str | None = None
    autoplay_chain_playlist_name: str | None = None
    autoplay_hunt_playlist_a_id: str | None = None
    autoplay_hunt_playlist_a_name: str | None = None
    autoplay_hunt_playlist_b_id: str | None = None
    autoplay_hunt_playlist_b_name: str | None = None
    if args.seed_rescue_playback:
        rows = build_rescue_playback(data_dir, own_beatgrid=args.seed_own_beatgrid)
        autoplay_chain_playlist_id = AUTOPLAY_CHAIN_PLAYLIST_ID
        autoplay_chain_playlist_name = AUTOPLAY_CHAIN_PLAYLIST_NAME
    elif args.seed_autoplay_hunt:
        rows = build_autoplay_hunt(data_dir)
        autoplay_hunt_playlist_a_id = HUNT_PLAYLIST_A_ID
        autoplay_hunt_playlist_a_name = HUNT_PLAYLIST_A_NAME
        autoplay_hunt_playlist_b_id = HUNT_PLAYLIST_B_ID
        autoplay_hunt_playlist_b_name = HUNT_PLAYLIST_B_NAME
    elif args.seed_autoplay_chain:
        rows = build_autoplay_chain(data_dir)
        autoplay_chain_playlist_id = AUTOPLAY_CHAIN_PLAYLIST_ID
        autoplay_chain_playlist_name = AUTOPLAY_CHAIN_PLAYLIST_NAME
    else:
        rows = build(data_dir, seed_playlists=args.seed_playlists)
    print(f"[OK] fixture library at {data_dir} with {len(rows)} track(s):")
    for stable_id, title, file_path in rows:
        print(f"  {stable_id}  {title!r}  {file_path}")
    if args.manifest is not None:
        manifest_path = Path(args.manifest).expanduser()
        if not manifest_path.is_absolute():
            raise SystemExit(f"[ERROR] --manifest must be absolute, got {args.manifest!r}")
        write_manifest(
            manifest_path,
            rows,
            autoplay_chain_playlist_id=autoplay_chain_playlist_id,
            autoplay_chain_playlist_name=autoplay_chain_playlist_name,
            autoplay_hunt_playlist_a_id=autoplay_hunt_playlist_a_id,
            autoplay_hunt_playlist_a_name=autoplay_hunt_playlist_a_name,
            autoplay_hunt_playlist_b_id=autoplay_hunt_playlist_b_id,
            autoplay_hunt_playlist_b_name=autoplay_hunt_playlist_b_name,
        )
        print(f"[OK] manifest written to {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
