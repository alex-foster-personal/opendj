from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT: Path = Path(__file__).resolve().parents[6]

#: Bump to invalidate every generated fixture dir. Anything that changes what
#: the audio SOUNDS like must bump this, or a stale dir keeps being measured.
FIXTURE_REVISION: int = 9
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

#: --seed-pairing-arrow only (pairing-arrow.spec.ts). The master deck loads
#: FIXTURE_TRACKS[0]; the PAIR arrow needs a paired partner that is in the
#: table but scrolled out of view, so the browse list must be longer than one
#: screen. Browse-only and never loaded; 30 s because
#: webkit-deckload-runway.test.mjs asserts every declared fixture track meets
#: its one-test runway floor. Kept out of FIXTURE_TRACKS so no other gate sees
#: the extra rows.
PAIRING_ARROW_BROWSE_TRACKS: tuple[FixtureTrack, ...] = tuple(
    FixtureTrack(filename=f"webkit-fixture-arrow-{n:02d}-120bpm.wav", bpm=120.0, seconds=30.0)
    for n in range(1, 25)
)
PAIRING_ARROW_TRACKS: tuple[FixtureTrack, ...] = (
    *FIXTURE_TRACKS,
    *PAIRING_ARROW_BROWSE_TRACKS,
)

MANIFEST_FILENAME: str = "fixture-manifest.json"

