"""The deep audio scan and the QLT letter (QLTSCAN-01, QLTSCAN-02).

[if] a file is damaged or lossy-sourced [then] the scan and letter say so, [else stop].

Real audio, written by ffmpeg into tmp_path at test time (pink noise, so the
spectrum is full band to start with), then damaged the way real files are:
a 128 kbps MP3 round trip, a 16 kHz encoder cut-off, clipping, truncation,
16-bit audio in a 24-bit container, a sharp upsample, mono, silence. Nothing
is mocked: `scan_file` decodes each one with the production ffmpeg path.

Regression lines:
- if a dark master (gentle roll-off) is marked down then broken (the maintainer's rule:
  a bad signal is worse than a missed flag)
- if a FLAC made from a 128 kbps MP3 does not grade C then broken
- if a FLAC brick-walled at 16 kHz does not grade C then broken
- if a clean 16-bit FLAC grades below A or carries an issue then broken
- if a clipped, truncated, padded-24-bit, mono or silence-padded file is
  missing its issue then broken
- if decode errors or truncation do not force D then broken
- if a stored facts record missing a key the letter needs grades as checked then broken
- if a shortfall at or under 1 s is flagged truncated, or one over it is not, then broken
- if an unscanned file reads as checked then broken
- if any threshold changes without the rule version changing then broken

-Claude
"""
from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from apps.shared import audio_grade, audio_quality, audio_scan
from apps.shared.audio_scan import (
    THRESHOLDS,
    ClipRuns,
    ScanCancelled,
    ScanError,
    ScanThresholds,
    find_cutoff,
    issues_of,
    rule_version,
    scan_file,
)

pytestmark = pytest.mark.requirement("QLTSCAN-01")

SECONDS = 8
NOISE = f"anoisesrc=d={SECONDS}:c=pink:r=44100:a=0.3"
NOISE_R = f"anoisesrc=d={SECONDS}:c=pink:r=44100:a=0.3:seed=7"


def _ffmpeg(*args: str) -> None:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture(scope="module")
def media(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Every fixture file, built once for the module."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    d = tmp_path_factory.mktemp("qlt")
    f = {name: d / name for name in (
        "clean.flac", "dark.flac", "fake128.flac", "brick16k.flac", "clipped.flac",
        "truncated.flac", "fake24.flac", "upsampled96.flac", "mono.flac", "lead.flac",
    )}
    stereo = ["-f", "lavfi", "-i", NOISE, "-f", "lavfi", "-i", NOISE_R]
    _ffmpeg(*stereo, "-filter_complex", "[0][1]amerge=inputs=2", "-sample_fmt", "s16", str(f["clean.flac"]))
    _ffmpeg(*stereo, "-filter_complex", "[0][1]amerge=inputs=2,lowpass=f=2500:poles=1", str(f["dark.flac"]))
    mp3 = d / "128.mp3"
    _ffmpeg("-i", str(f["clean.flac"]), "-c:a", "libmp3lame", "-b:a", "128k", str(mp3))
    _ffmpeg("-i", str(mp3), "-sample_fmt", "s16", str(f["fake128.flac"]))
    m16 = d / "16k.mp3"
    _ffmpeg("-i", str(f["clean.flac"]), "-c:a", "libmp3lame", "-b:a", "128k", "-cutoff", "16000", str(m16))
    _ffmpeg("-i", str(m16), "-sample_fmt", "s16", str(f["brick16k.flac"]))
    _ffmpeg("-i", str(f["clean.flac"]), "-af",
            "volume=18dB,alimiter=limit=1:level=false:attack=0.1:release=1,volume=6dB",
            "-sample_fmt", "s16", str(f["clipped.flac"]))
    whole = f["clean.flac"].read_bytes()
    f["truncated.flac"].write_bytes(whole[: len(whole) // 2])
    _ffmpeg("-i", str(f["clean.flac"]), "-sample_fmt", "s32", str(f["fake24.flac"]))
    _ffmpeg("-i", str(f["clean.flac"]), "-af", "aresample=96000:resampler=soxr:precision=28",
            "-sample_fmt", "s32", str(f["upsampled96.flac"]))
    _ffmpeg("-f", "lavfi", "-i", NOISE, "-ac", "2", "-sample_fmt", "s16", str(f["mono.flac"]))
    _ffmpeg("-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo:d=4", "-i", str(f["clean.flac"]),
            "-filter_complex", "[0][1]concat=n=2:v=0:a=1", "-sample_fmt", "s16", str(f["lead.flac"]))
    return f


def _grade(path: Path) -> audio_grade.Grade:
    facts = scan_file(path).as_dict()
    quality = audio_quality.classify(str(path), int(facts["header_duration_s"] * 1000)).as_dict()
    return audio_grade.grade(quality, facts)


# ----------------------------------------------------------- the scanner


@pytest.mark.requires_ffmpeg
def test_clean_flac_is_full_band_with_no_issues(media: dict[str, Path]) -> None:
    facts = scan_file(media["clean.flac"])
    assert facts.brick_wall is False
    assert facts.cutoff_hz is not None and facts.cutoff_hz > 21000
    assert facts.issues == []
    assert facts.decode_errors == 0 and facts.truncated_s == 0.0
    assert facts.padded_24bit is None, "a 16-bit file cannot be padded 24-bit"


@pytest.mark.requires_ffmpeg
def test_dark_master_is_never_cut(media: dict[str, Path]) -> None:
    facts = scan_file(media["dark.flac"])
    assert facts.brick_wall is False, facts
    assert _grade(media["dark.flac"]).letter == "A"


@pytest.mark.requires_ffmpeg
def test_flac_from_a_128k_mp3_is_cut_near_17k_and_grades_c(media: dict[str, Path]) -> None:
    facts = scan_file(media["fake128.flac"])
    assert facts.brick_wall is True
    assert facts.cutoff_hz is not None and 16000 <= facts.cutoff_hz < 17000, facts.cutoff_hz
    grade = _grade(media["fake128.flac"])
    assert (grade.letter, grade.checked) == ("C", True)
    assert "likely made from a 128 kbps MP3" in "\n".join(grade.hover)


@pytest.mark.requires_ffmpeg
def test_flac_brick_walled_at_16k_grades_c(media: dict[str, Path]) -> None:
    facts = scan_file(media["brick16k.flac"])
    assert facts.brick_wall is True
    assert facts.cutoff_hz is not None and 15500 <= facts.cutoff_hz < 16600
    assert _grade(media["brick16k.flac"]).letter == "C"


@pytest.mark.requires_ffmpeg
def test_clipping_is_counted_at_the_files_own_peak(media: dict[str, Path]) -> None:
    facts = scan_file(media["clipped.flac"])
    assert "clipping" in facts.issues
    assert facts.clip_runs >= THRESHOLDS.clip_min_runs_to_flag
    grade = _grade(media["clipped.flac"])
    assert grade.letter == "A", "clipping is an issue dot, not a letter change"
    assert "flat-topped runs" in grade.hover[-1]


@pytest.mark.requires_ffmpeg
def test_truncated_file_has_decode_errors_and_grades_d(media: dict[str, Path]) -> None:
    facts = scan_file(media["truncated.flac"])
    assert facts.truncated_s > THRESHOLDS.truncation_flag_s
    assert "truncated" in facts.issues
    assert _grade(media["truncated.flac"]).letter == "D"


@pytest.mark.requires_ffmpeg
def test_16_bit_audio_in_a_24_bit_container_is_padded(media: dict[str, Path]) -> None:
    facts = scan_file(media["fake24.flac"])
    assert facts.bit_depth == 24
    assert facts.padded_24bit is True
    assert "padded_24bit" in facts.issues


@pytest.mark.requires_ffmpeg
def test_sharp_upsample_is_flagged(media: dict[str, Path]) -> None:
    facts = scan_file(media["upsampled96.flac"])
    assert facts.sample_rate == 96000
    assert facts.upsampled is True
    assert "upsampled" in facts.issues


@pytest.mark.requires_ffmpeg
def test_mono_in_stereo_and_lead_silence(media: dict[str, Path]) -> None:
    assert "mono_in_stereo" in scan_file(media["mono.flac"]).issues
    assert "mono_in_stereo" not in scan_file(media["clean.flac"]).issues
    lead = scan_file(media["lead.flac"])
    assert 3.5 <= lead.lead_silence_s <= 4.5
    assert "silence_at_start" in lead.issues
    assert "silence_at_end" not in lead.issues


@pytest.mark.requires_ffmpeg
def test_a_fade_tail_is_not_padding_but_a_long_one_is(media: dict[str, Path]) -> None:
    clean = scan_file(media["clean.flac"])
    assert "silence_at_end" not in issues_of(dataclasses.replace(clean, trail_silence_s=6.0))
    assert "silence_at_end" in issues_of(dataclasses.replace(clean, trail_silence_s=12.0))
    assert "silence_at_start" in issues_of(dataclasses.replace(clean, lead_silence_s=2.5))


@pytest.mark.requires_ffmpeg
def test_garbage_raises_scan_error(tmp_path: Path) -> None:
    junk = tmp_path / "junk.mp3"
    junk.write_bytes(b"\x00not audio" * 400)
    with pytest.raises(ScanError):
        scan_file(junk)


# ------------------------------------------------------ pure functions


def _psd(rate: int, edge_hz: float, fall_db: float, width_hz: float) -> np.ndarray:
    freqs = np.fft.rfftfreq(THRESHOLDS.nfft, 1 / rate)
    level = np.where(freqs < edge_hz, 0.0, -fall_db * np.clip((freqs - edge_hz) / width_hz, 0, 1))
    return 10 ** (level / 10)


def test_find_cutoff_steep_edge_versus_gentle_rolloff() -> None:
    edge, steep = find_cutoff(_psd(44100, 16000, 80, 200), 44100)
    assert edge is not None and 15800 <= edge <= 16300
    assert steep is not None and steep >= THRESHOLDS.steep_min_db
    _edge, gentle = find_cutoff(_psd(44100, 9000, 50, 12000), 44100)
    assert gentle is None or gentle < THRESHOLDS.steep_min_db


def test_a_cut_off_below_8k_is_found_and_counted() -> None:
    edge, steep = find_cutoff(_psd(44100, 6000, 80, 200), 44100)
    assert edge is not None and 5800 <= edge <= 6300
    assert steep is not None and steep >= THRESHOLDS.steep_min_db
    assert audio_grade.spectral_cap(round(edge), True) == "D"


def test_a_cut_off_below_the_primary_reference_band_is_found() -> None:
    """Cut at 3 kHz: the 4-8 kHz band is itself stopband, so the low reference decides."""
    edge, steep = find_cutoff(_psd(44100, 3000, 80, 200), 44100)
    assert edge is not None and 2800 <= edge <= 3300, edge
    assert steep is not None and steep >= THRESHOLDS.steep_min_db


def test_out_of_phase_treble_is_not_read_as_a_cut_off() -> None:
    """L = low + high, R = low - high: the mono sum holds only the low half."""
    rng = np.random.default_rng(3)
    rate, n = 44100, 44100 * 4
    spectrum = np.fft.rfft(rng.standard_normal(n) * 0.1)
    freqs = np.fft.rfftfreq(n, 1 / rate)
    low = np.fft.irfft(np.where(freqs < 4000, spectrum, 0), n)
    high = np.fft.irfft(np.where(freqs >= 4000, spectrum, 0), n)
    fold = audio_scan._Fold(2, THRESHOLDS)
    fold.feed(np.stack([low + high, low - high], axis=1), None)
    edge, _steep, brick = fold.cutoff(rate)
    assert brick is False and edge is not None and edge > 20000, (edge, brick)


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX FIFO")
@pytest.mark.requires_ffmpeg
def test_a_stalled_decode_is_killed_at_the_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fifo = tmp_path / "stalled.wav"
    os.mkfifo(fifo)  # nothing ever writes, so ffmpeg blocks opening it
    monkeypatch.setattr(audio_scan, "DECODE_TIMEOUT_S", 1.0)
    with pytest.raises(ScanError, match="no progress"):
        audio_scan._decode(fifo, 2, False, audio_scan._Fold(2, THRESHOLDS))


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX FIFO")
@pytest.mark.requires_ffmpeg
def test_a_cancel_kills_a_decode_stalled_in_a_blocked_read(tmp_path: Path) -> None:
    fifo = tmp_path / "stalled.wav"
    os.mkfifo(fifo)
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(ScanCancelled):
        audio_scan._decode(fifo, 2, False, audio_scan._Fold(2, THRESHOLDS), cancel)
    assert time.monotonic() - started < 5, "cancel waited for the 900 s deadline"


@pytest.mark.requires_ffmpeg
def test_a_cancel_kills_the_decode_in_flight(media: dict[str, Path]) -> None:
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ScanCancelled):
        scan_file(media["clean.flac"], cancel=cancel)


def test_clip_runs_count_consecutive_frames_at_peak_only_across_chunks() -> None:
    clips = ClipRuns()
    first = np.zeros(20)
    first[[10, 11, 12]] = 1.0  # a run of 3 at the peak
    first[[18, 19]] = 1.0  # a run that continues into the next chunk
    second = np.zeros(20)
    second[[0, 1]] = 1.0  # ... making it 4 long
    second[[5, 6, 7]] = 0.95  # 3 in a row, but below the peak
    clips.feed(first, 0)
    clips.feed(second, 20)
    runs, longest = clips.result(1.0, 44100)
    assert runs == 2, "10-12 and 18-21; the 0.95 run is not at the peak"
    assert longest == round(4 / 44100 * 1000, 3)
    assert clips.result(0.5, 44100) == (0, 0.0), "below -1 dBFS nothing is clipping"


def test_clip_counting_memory_does_not_grow_with_the_file() -> None:
    clips = ClipRuns()
    size = clips.closed.nbytes + clips.longest.nbytes + clips.open_len.nbytes + clips.open_end.nbytes
    square = np.ones(88200)
    for chunk in range(50):
        clips.feed(square, chunk * 88200)
    after = clips.closed.nbytes + clips.longest.nbytes + clips.open_len.nbytes + clips.open_end.nbytes
    assert after == size
    assert clips.result(1.0, 44100)[0] == 1, "one unbroken flat top"


def test_every_threshold_moves_the_rule_version() -> None:
    base = rule_version()
    for field in dataclasses.fields(ScanThresholds):
        value = getattr(THRESHOLDS, field.name)
        bumped = (value[0] + 1, value[1]) if isinstance(value, tuple) else value + 1
        changed = ScanThresholds(**{**dataclasses.asdict(THRESHOLDS), field.name: bumped})
        assert rule_version(changed) != base, field.name


# --------------------------------------------------------------- grade


def _quality(venue: str, container: str = ".flac", kbps: int = 900) -> dict:
    lossless = container == ".flac"
    return {"venue": venue, "label": venue, "rank": 5, "of": 6, "blurb": "", "kbps": kbps,
            "container": container, "lossless": lossless}


def _facts(**over: object) -> dict:
    base = {"cutoff_hz": 21900, "brick_wall": False, "issues": [], "channels": 2,
            "sample_rate": 44100, "bit_depth": 16, "claimed_kbps": None}
    return {**base, **over}


@pytest.mark.parametrize(
    ("cutoff", "letter"),
    [(14999, "D"), (15000, "C"), (16800, "C"), (16999, "C"), (17000, "B"), (18999, "B"), (19000, "A")],
)
def test_brick_wall_caps_at_the_agreed_lines(cutoff: int, letter: str) -> None:
    got = audio_grade.grade(_quality("stadium"), _facts(cutoff_hz=cutoff, brick_wall=True))
    assert got.letter == letter


def test_spectrum_only_lowers_and_needs_a_brick_wall() -> None:
    assert audio_grade.grade(_quality("lounge", ".mp3", 160), _facts(cutoff_hz=20000, brick_wall=True)).letter == "C"
    assert audio_grade.grade(_quality("stadium"), _facts(cutoff_hz=9000, brick_wall=False)).letter == "A"


def test_corruption_forces_d_and_other_issues_do_not_move_the_letter() -> None:
    assert audio_grade.grade(_quality("warehouse", ".mp3", 320), _facts(issues=["decode_errors"], decode_errors=3)).letter == "D"
    clipped = audio_grade.grade(_quality("stadium"), _facts(issues=["clipping"], clip_runs=40, clip_longest_ms=0.4))
    assert clipped.letter == "A" and clipped.issues == ("clipping",)


def test_unscanned_is_the_bitrate_letter_unchecked() -> None:
    got = audio_grade.grade(_quality("house_party", ".mp3", 224), None)
    assert (got.letter, got.checked) == ("B", False)
    assert got.hover[0].startswith("B (not checked yet)")
    assert "Bandwidth: not checked" in got.hover


def test_a_failed_scan_says_so_and_is_unchecked() -> None:
    got = audio_grade.grade(_quality("stadium"), None, scan_error="no audio decoded")
    assert got.checked is False
    assert got.hover[-1] == "Issues: scan failed -- no audio decoded"


def test_unknown_venue_has_no_letter() -> None:
    got = audio_grade.grade({**_quality("stadium"), "venue": None, "blurb": "file missing"}, None)
    assert (got.letter, got.checked) == (None, False)


def test_b_c_line_is_17k_and_versioned() -> None:
    assert audio_grade.THRESHOLDS.c_below_hz == 17000
    moved = audio_grade.GradeThresholds(c_below_hz=16500)
    assert audio_grade.grade_rule_version(moved) != audio_grade.grade_rule_version()


@pytest.mark.parametrize("missing", ["issues", "cutoff_hz", "brick_wall"])
def test_incomplete_stored_facts_are_never_checked(missing: str) -> None:
    facts = _facts(cutoff_hz=16000, brick_wall=True)
    del facts[missing]
    got = audio_grade.grade(_quality("stadium"), facts)
    assert got.checked is False and got.issues == ()
    assert any(missing in line and "incomplete" in line for line in got.hover), got.hover


def test_complete_facts_control_is_checked() -> None:
    got = audio_grade.grade(_quality("stadium"), _facts(cutoff_hz=16000, brick_wall=True))
    assert got.checked is True and got.letter == "C"


def test_truncation_is_flagged_only_past_the_one_second_tolerance(media: dict[str, Path]) -> None:
    clean = scan_file(media["clean.flac"])
    assert THRESHOLDS.truncation_flag_s == 1.0
    assert "truncated" not in issues_of(dataclasses.replace(clean, truncated_s=0.4))
    assert "truncated" not in issues_of(dataclasses.replace(clean, truncated_s=1.0))
    assert "truncated" in issues_of(dataclasses.replace(clean, truncated_s=1.01))


def test_a_header_with_no_duration_is_a_scan_error_not_a_complete_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[if] a missing header duration grades truncation as 0 s [then] broken, [else stop]."""
    monkeypatch.setattr(audio_scan, "_header", lambda _path: (44100, 2, 16, None, None))
    with pytest.raises(ScanError, match="no duration"):
        audio_scan.scan_file(tmp_path / "no-duration.flac")


def test_clipping_runs_are_counted_per_signed_channel() -> None:
    """Peaks alternating between channels or polarities are not a flat top."""
    fold = audio_scan._Fold(2, THRESHOLDS)
    block = np.zeros((40, 2))
    block[10, 0], block[11, 1], block[12, 0] = 1.0, 1.0, 1.0  # L, R, L
    block[20, 0], block[21, 0], block[22, 0] = 1.0, -1.0, 1.0  # +, -, +
    fold.feed(block, None)
    assert fold.clipping(44100)[0] == 0
    block[30:33, 1] = -1.0  # a real negative flat top on the right channel
    fold2 = audio_scan._Fold(2, THRESHOLDS)
    fold2.feed(block, None)
    assert fold2.clipping(44100)[0] == 1


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX FIFO")
@pytest.mark.requires_ffmpeg
def test_a_decode_that_keeps_progressing_outlives_the_stall_deadline(
    media: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deadline is inactivity, not total wall time.

    The watchdog reads a virtual clock that moves only when a chunk is fed, so
    every gap between chunks is exactly `step_s` and the total runs far past
    the deadline. A slow or loaded host (ffmpeg start-up, a starved thread)
    cannot move that clock, so the margin is no longer a race against load.
    """
    deadline_s, step_s = 0.3, 0.1

    class _FeedClock:
        """`time`, except monotonic() is virtual and logs the watchdog's reads."""

        def __init__(self) -> None:
            self.now = 0.0
            self.watchdog_reads: list[float] = []

        def monotonic(self) -> float:
            if threading.current_thread().name == "audio-scan-stall":
                self.watchdog_reads.append(self.now)
            return self.now

        def __getattr__(self, name: str) -> object:
            return getattr(time, name)

    clock = _FeedClock()
    real_feed = audio_scan._Fold.feed

    def slow_feed(self, block, raw):  # type: ignore[no-untyped-def]
        time.sleep(0.02)  # real time, so the watchdog polls while chunks arrive
        clock.now += step_s
        real_feed(self, block, raw)

    monkeypatch.setattr(audio_scan, "time", clock)
    monkeypatch.setattr(audio_scan, "DECODE_TIMEOUT_S", deadline_s)
    monkeypatch.setattr(audio_scan._Fold, "feed", slow_feed)
    monkeypatch.setattr(audio_scan, "CHUNK_SECONDS", 0.05)
    fold = audio_scan._Fold(2, THRESHOLDS)
    audio_scan._decode(media["clean.flac"], 2, False, fold)
    assert fold.frames > 0
    assert clock.now > 10 * deadline_s, f"decode lasted only {clock.now} virtual s"
    assert clock.watchdog_reads and max(clock.watchdog_reads) > 3 * deadline_s, (
        f"the watchdog never checked after the deadline had passed: {clock.watchdog_reads}"
    )
