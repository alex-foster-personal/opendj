"""Measure one audio file's real quality: one ffmpeg decode plus numpy (QLTSCAN-01).

`audio_quality.py` grades from size and extension with no decode. This module
is its deep counterpart: it decodes the whole file once and measures what a
header cannot show. Every signal here is one the decision doc
(`(private path)`) ranks as reliable:

- header facts (tinytag, in-process): sample rate, bit depth, channels,
  claimed bitrate, header duration
- decode errors and truncation (ffmpeg's own `error` lines, decoded length vs
  header length)
- spectral cutoff WITH a sharp-drop test: only a brick-wall edge (what a lossy
  encoder leaves) counts. A dark master rolls off gently and is not cut.
- hard clipping: runs of 3+ consecutive samples at the file's own peak
- DC offset, mono content in a stereo file, silence over 2 s at the start
  or over 10 s at the end (a fade tail is not padding)
- fake 24-bit (low 16 bits of every s32 sample zero) and upsampled hi-res

WHY ONE STREAMED DECODE. Everything is folded chunk by chunk from ffmpeg's
stdout, so ffmpeg is spawned once per file. ffmpeg runs as a separate process
(not linked), the same arm's-length use as `apps/loudness/scan.py`, which
keeps the Apache-2.0 licence clean. It starts through the platform priority
seam (`apps/shared/process_priority.py`: niceness 10, below-normal on Windows,
background QoS on macOS) so a library scan never competes with playback.

NOT here: loudness, LRA and true peak (`apps/loudness/scan.py` owns them, and
true peak over 0 dBTP is headroom, not damage), and the letter grade
(`audio_grade.py`).

MINI-PRD
--------
  ✔︎ ✅ 🎯 scan_file(): measured facts for one file, or a ScanError.
    [if] a FLAC carries a brick-wall at 16 kHz [then] cutoff_hz ~16000, brick_wall
    [if] a lossless master rolls off gently [then] brick_wall False ⛔️ a dark
         master marked down
    [if] the file is cut short [then] truncated or decode errors are reported
    [if] ffmpeg is missing [then ⛔️] FfmpegUnavailable, never a partial result

-Claude
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import IO

import numpy as np

from apps.shared import _tagreader, process_priority
from apps.shared.ffmpeg import resolve_ffmpeg

# ----- CFG -------------------------------------------------------------------
SCAN_ALGORITHM = "audio-scan-2"
AF_SERVICE_ID = "com.opendj.audio-quality-scan"
LOSSLESS_CONTAINERS: frozenset[str] = frozenset({".flac", ".wav", ".aiff", ".aif", ".alac", ".ape", ".wv"})
DECODE_TIMEOUT_S = 900
DECODE_NICENESS = 10
CANCEL_POLL_S = 0.25
CHUNK_SECONDS = 2.0


@dataclass(frozen=True)
class ScanThresholds:
    """Every tunable number. A change here changes `rule_version()`."""

    nfft: int = 8192
    # A frame quieter than this (RMS, full scale 1.0) says nothing about bandwidth.
    spectrum_min_rms: float = 1e-3
    # Reference level: median of this band. Edge = highest frequency whose
    # smoothed level is within `edge_drop_db` of it.
    ref_band_hz: tuple[float, float] = (4000.0, 8000.0)
    # Second reference, used only when nothing above 8 kHz is near the first.
    low_ref_band_hz: tuple[float, float] = (500.0, 2000.0)
    edge_drop_db: float = 45.0
    smooth_hz: float = 120.0
    # The sharp-drop test: level just below the edge minus level just above
    # it, each averaged over `steep_window_hz`, starting `steep_gap_hz` away.
    steep_gap_hz: float = 150.0
    steep_window_hz: float = 600.0
    steep_min_db: float = 25.0
    # Clipping: runs of this many samples within `clip_tolerance` of the
    # file's own peak, counted only when that peak is at least `clip_min_peak`.
    clip_min_run: int = 3
    clip_tolerance: float = 0.001
    clip_min_peak: float = 0.89  # -1 dBFS
    clip_min_runs_to_flag: int = 10
    # Top of the clipping ladder (ClipRuns): +6 dBFS covers lossy decode overshoot.
    clip_ladder_top: float = 2.0
    dc_offset_flag: float = 0.01  # 1% of full scale
    mono_side_db: float = -60.0
    silence_dbfs: float = -60.0
    # Leading silence matters for cueing. A trailing one is usually a fade or
    # reverb tail under -60 dBFS (2-6 s on 46 of 200 library files, Tue 27 Oct
    # 2026), so only a long tail counts.
    lead_silence_flag_s: float = 2.0
    trail_silence_flag_s: float = 10.0
    #: Decoded audio this much shorter than the header's length is `truncated`.
    #: Header durations are estimates (encoder delay and padding, VBR frame
    #: counts) that miss a clean file by a fraction of a second, so a smaller
    #: shortfall is not damage and is not flagged.
    truncation_flag_s: float = 1.0
    # A hi-res file whose brick-wall sits below this is upsampled.
    upsampled_max_cutoff_hz: float = 24500.0


THRESHOLDS = ScanThresholds()


def rule_version(thresholds: ScanThresholds = THRESHOLDS) -> str:
    """Changes with any threshold or the algorithm, so stored facts go stale."""
    digest = hashlib.sha256(json.dumps(asdict(thresholds), sort_keys=True).encode("utf-8")).hexdigest()
    return f"{SCAN_ALGORITHM}:{digest[:12]}"


class ScanCancelled(RuntimeError):
    """The caller's cancel event was set while the file was decoding."""


class ScanError(RuntimeError):
    """The file could not be measured at all (unreadable header, no audio)."""


@dataclass(frozen=True)
class ScanFacts:
    """What was measured. `None` always means "not measured", never "fine"."""

    container: str
    lossless: bool
    sample_rate: int
    channels: int
    bit_depth: int | None
    claimed_kbps: float | None
    header_duration_s: float | None
    decoded_duration_s: float
    decode_errors: int
    first_decode_error: str | None
    truncated_s: float
    cutoff_hz: int | None
    cutoff_steep_db: float | None
    brick_wall: bool
    peak: float
    clip_runs: int
    clip_longest_ms: float
    dc_offset: float
    mono_in_stereo: bool | None
    lead_silence_s: float
    trail_silence_s: float
    padded_24bit: bool | None
    upsampled: bool | None
    decode_seconds: float
    analysis_seconds: float
    issues: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


# ----- decode helpers --------------------------------------------------------
def _ffmpeg_argv(path: Path, integer_pcm: bool) -> list[str]:
    fmt = "s32le" if integer_pcm else "f32le"
    return [
        resolve_ffmpeg(),
        "-nostdin",
        "-hide_banner",
        "-v",
        "error",
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-f",
        fmt,
        "-",
    ]


def _spawn(argv: list[str], stderr: IO[bytes]) -> subprocess.Popen[bytes]:
    """ffmpeg at background priority through the platform seam."""
    return subprocess.Popen(
        process_priority.background_argv(argv),
        stdout=subprocess.PIPE,
        stderr=stderr,
        env={**os.environ, "AF_SERVICE_ID": AF_SERVICE_ID},
        **process_priority.lowered_priority(DECODE_NICENESS),
    )


class _Fold:
    """Running measurements, fed one interleaved chunk at a time."""

    def __init__(self, channels: int, thresholds: ScanThresholds) -> None:
        self.t = thresholds
        self.channels = channels
        self.frames = 0
        self.sum = np.zeros(channels)
        self.peak = 0.0
        self.mid_energy = 0.0
        self.side_energy = 0.0
        self.psd = np.zeros(thresholds.nfft // 2 + 1)
        self.psd_frames = 0
        self.window = np.hanning(thresholds.nfft)
        self.carry = np.zeros((0, channels))
        self.low16_seen = False
        self.loud_first: int | None = None
        self.loud_last = -1
        # One run counter per channel and polarity: a flat top is a run in ONE
        # signed channel, never peaks alternating between channels or signs.
        self.clips = [ClipRuns(thresholds) for _ in range(2 * channels)]

    def feed(self, block: np.ndarray, raw_int: np.ndarray | None) -> None:
        if raw_int is not None and not self.low16_seen:
            self.low16_seen = bool(np.any(raw_int & 0xFFFF))
        n = block.shape[0]
        self.sum += block.sum(axis=0)
        mono = block.mean(axis=1)
        absmax = np.abs(block).max(axis=1)
        if n:
            self.peak = max(self.peak, float(absmax.max()))
        if self.channels == 2:
            self.mid_energy += float(np.sum(mono**2))
            self.side_energy += float(np.sum(((block[:, 0] - block[:, 1]) / 2) ** 2))
        floor = 10 ** (self.t.silence_dbfs / 20)
        loud = np.nonzero(absmax > floor)[0]
        if loud.size:
            if self.loud_first is None:
                self.loud_first = self.frames + int(loud[0])
            self.loud_last = self.frames + int(loud[-1])
        for ch in range(self.channels):
            self.clips[2 * ch].feed(block[:, ch], self.frames)
            self.clips[2 * ch + 1].feed(-block[:, ch], self.frames)
        self._spectrum(block)
        self.frames += n

    def cutoff(self, rate: int) -> tuple[float | None, float | None, bool]:
        """`(edge_hz, steepness_db, brick_wall)` of the averaged spectrum."""
        if not self.psd_frames:
            return None, None, False
        edge, steep = find_cutoff(self.psd / self.psd_frames, rate, self.t)
        return edge, steep, steep is not None and steep >= self.t.steep_min_db

    def padded_24bit(self, lossless: bool, depth: int | None) -> bool | None:
        """A 24-bit lossless file whose low 16 bits were never set; None otherwise."""
        if not lossless or not depth or depth < 24:
            return None
        return not self.low16_seen

    def clipping(self, rate: int) -> tuple[int, float]:
        results = [clips.result(self.peak, rate) for clips in self.clips]
        return sum(r[0] for r in results), max((r[1] for r in results), default=0.0)

    def mono_verdict(self) -> bool | None:
        """Side energy far below mid; None when not a stereo file."""
        if self.channels != 2 or self.mid_energy <= 0:
            return None
        return bool(10 * np.log10(self.side_energy / self.mid_energy + 1e-30) < self.t.mono_side_db)

    def silence_ends(self, rate: int) -> tuple[float, float]:
        """Seconds below the silence floor at the start and the end."""
        if self.loud_first is None:
            whole = self.frames / rate
            return whole, whole
        return self.loud_first / rate, (self.frames - 1 - self.loud_last) / rate

    def _spectrum(self, block: np.ndarray) -> None:
        """Power per channel, summed: measured before any downmix, so treble
        that is out of phase between the channels is never cancelled away."""
        nfft = self.t.nfft
        x = np.concatenate([self.carry, block])
        usable = (len(x) // nfft) * nfft
        if usable:
            # (frames, nfft, channels) -> one row per (frame, channel)
            frames = x[:usable].reshape(-1, nfft, self.channels).transpose(0, 2, 1).reshape(-1, nfft)
            rms = np.sqrt(np.mean(frames**2, axis=1))
            frames = frames[rms >= self.t.spectrum_min_rms]
            if len(frames):
                spec = np.abs(np.fft.rfft(frames * self.window, axis=1)) ** 2
                self.psd += spec.sum(axis=0)
                self.psd_frames += len(frames)
        self.carry = x[usable:]


class ClipRuns:
    """Runs of `clip_min_run`+ consecutive frames at the file's own peak, in
    bounded memory.

    The peak is only known at the end, so runs are counted on a fixed ladder of
    levels at `clip_tolerance` spacing from just under -1 dBFS up to +6 dBFS
    (lossy decodes overshoot full scale). Each rung keeps four numbers: runs
    closed, longest run, and the run still open at the chunk edge. The answer is
    read from the highest rung at or under `peak * (1 - clip_tolerance)`, so the
    threshold used is within one tolerance step of the exact one. Memory is a
    few thousand numbers whatever the file's length or how clipped it is.
    """

    def __init__(self, t: ScanThresholds = THRESHOLDS) -> None:
        self.t = t
        self.floor = t.clip_min_peak * (1 - t.clip_tolerance)
        self.step = 1 + t.clip_tolerance
        rungs = int(np.ceil(np.log(t.clip_ladder_top / self.floor) / np.log(self.step))) + 1
        self.levels = self.floor * self.step ** np.arange(rungs)
        self.closed = np.zeros(rungs, dtype=np.int64)
        self.longest = np.zeros(rungs, dtype=np.int64)
        self.open_len = np.zeros(rungs, dtype=np.int64)
        self.open_end = np.full(rungs, -2, dtype=np.int64)

    def _close(self, rung: int, length: int) -> None:
        if length >= self.t.clip_min_run:
            self.closed[rung] += 1
            self.longest[rung] = max(self.longest[rung], length)

    def feed(self, absmax: np.ndarray, first_frame: int) -> None:
        """Fold one chunk of one signed channel (or any per-frame level); frame
        indices start at `first_frame`. Values below the floor, negatives
        included, are never hot."""
        hot = np.nonzero(absmax >= self.floor)[0]
        top = int(np.searchsorted(self.levels, float(absmax[hot].max()), side="right")) if hot.size else 0
        last_frame = first_frame + len(absmax) - 1
        # Rungs this chunk never reaches: any open run ended before it.
        for rung in np.nonzero(self.open_len[top:])[0] + top:
            self._close(int(rung), int(self.open_len[rung]))
            self.open_len[rung] = 0
        for rung in range(top):
            self._feed_rung(rung, hot[absmax[hot] >= self.levels[rung]] + first_frame, last_frame)

    def _feed_rung(self, rung: int, frames: np.ndarray, last_frame: int) -> None:
        breaks = np.nonzero(np.diff(frames) != 1)[0]
        lengths = np.diff(np.concatenate([[0], breaks + 1, [frames.size]]))
        if self.open_len[rung] and frames[0] == self.open_end[rung] + 1:
            lengths[0] += self.open_len[rung]
        elif self.open_len[rung]:
            self._close(rung, int(self.open_len[rung]))
        still_open = frames[-1] == last_frame
        for length in lengths[:-1] if still_open else lengths:
            self._close(rung, int(length))
        self.open_len[rung] = int(lengths[-1]) if still_open else 0
        self.open_end[rung] = int(frames[-1])

    def result(self, peak: float, rate: int) -> tuple[int, float]:
        """`(runs, longest_ms)` at this file's peak; nothing below -1 dBFS."""
        if peak < self.t.clip_min_peak:
            return 0, 0.0
        rung = int(np.searchsorted(self.levels, peak * (1 - self.t.clip_tolerance), side="right")) - 1
        runs = int(self.closed[rung]) + (1 if self.open_len[rung] >= self.t.clip_min_run else 0)
        open_run = int(self.open_len[rung]) if self.open_len[rung] >= self.t.clip_min_run else 0
        longest = max(int(self.longest[rung]), open_run)
        return runs, round(longest / rate * 1000, 3)


def _edge_index(
    freqs: np.ndarray,
    db: np.ndarray,
    smoothed: np.ndarray,
    band: tuple[float, float],
    top: float,
    t: ScanThresholds,
) -> int | None:
    """Highest bin above `band` whose smoothed level is within `edge_drop_db`
    of the band's median level, or None."""
    ref = float(np.median(db[(freqs >= band[0]) & (freqs < band[1])]))
    valid = (freqs > band[1]) & (freqs < top)
    above = np.nonzero(valid & (smoothed > ref - t.edge_drop_db))[0]
    return int(above[-1]) if above.size else None


def find_cutoff(psd: np.ndarray, rate: int, t: ScanThresholds = THRESHOLDS) -> tuple[float | None, float | None]:
    """`(edge_hz, steepness_db)` of an averaged power spectrum.

    The edge is the highest frequency still within `edge_drop_db` of the
    4-8 kHz reference. Steepness is the level just below the edge minus the
    level just above it: a lossy encoder's low-pass gives 40+ dB over a few
    hundred hertz, a dark master's natural roll-off a few dB. Steepness is
    None when the edge sits at Nyquist (full band). Pure function, so the
    classifier fixtures can drive it directly.
    """
    freqs = np.fft.rfftfreq(t.nfft, 1 / rate)
    db = 10 * np.log10(psd + 1e-20)
    bin_hz = float(freqs[1])
    width = max(1, round(t.smooth_hz / bin_hz))
    smoothed = np.convolve(db, np.ones(width) / width, mode="same")
    # Ignore the top smoothing width: the convolution's edge effect lives there.
    top = float(freqs[-1] - width * bin_hz)
    index = _edge_index(freqs, db, smoothed, t.ref_band_hz, top, t)
    # The 0.5-2 kHz reference is always evaluated too. When its edge lands below
    # the 4-8 kHz band, that band sits in the stopband itself (a source cut at
    # 2-4 kHz), so the edge measured from it is meaningless (it reads as full
    # band) and the low-reference edge is the real one. Likewise when nothing
    # above 8 kHz is near the 4-8 kHz level (a very low bitrate source).
    low_index = _edge_index(freqs, db, smoothed, t.low_ref_band_hz, top, t)
    if index is None or (low_index is not None and freqs[low_index] < t.ref_band_hz[0]):
        index = low_index
    if index is None:
        return float(t.low_ref_band_hz[1]), None
    edge = float(freqs[index])
    gap, win = t.steep_gap_hz, t.steep_window_hz
    below_win = (freqs >= edge - gap - win) & (freqs < edge - gap)
    above_win = (freqs > edge + gap) & (freqs <= edge + gap + win)
    if not above_win.any() or freqs[above_win][-1] < edge + gap + win - 2 * bin_hz:
        return edge, None
    steep = float(np.mean(db[below_win]) - np.mean(db[above_win]))
    return edge, round(steep, 1)


# ----- scan ------------------------------------------------------------------
def _header(path: Path) -> tuple[int, int, int | None, float | None, float | None]:
    try:
        tag = _tagreader.read(path)
    except _tagreader.TagReadError as exc:
        raise ScanError(f"header unreadable: {exc}") from exc
    if not tag.samplerate or not tag.channels:
        raise ScanError("header has no sample rate or channel count")
    depth = getattr(tag, "bitdepth", None) or None
    kbps = float(tag.bitrate) if tag.bitrate else None
    duration = float(tag.duration) if tag.duration else None
    return int(tag.samplerate), int(tag.channels), depth, kbps, duration


def _watch_cancel(proc: subprocess.Popen[bytes], cancel: threading.Event) -> None:
    """Kill `proc` the moment `cancel` is set, even mid-way through a blocked
    read (a stalled volume). The watcher ends with the process."""

    def _watch() -> None:
        while proc.poll() is None:
            if cancel.wait(CANCEL_POLL_S):
                proc.kill()
                return

    threading.Thread(target=_watch, name="audio-scan-cancel", daemon=True).start()


def _decode(
    path: Path, channels: int, integer_pcm: bool, fold: _Fold, cancel: threading.Event | None = None
) -> tuple[list[str], int, float]:
    """Stream-decode into `fold`. Returns (error lines, exit code, analysis s)."""
    analysis = 0.0
    chunk_bytes = int(48000 * CHUNK_SECONDS) * channels * 4
    frame_bytes = 4 * channels
    with tempfile.TemporaryFile() as err:
        proc = _spawn(_ffmpeg_argv(path, integer_pcm), err)
        assert proc.stdout is not None
        # A stalled read (a volume gone unresponsive) never returns to a check
        # in this loop, so a watcher thread kills the decoder once no chunk has
        # arrived for DECODE_TIMEOUT_S. Progress resets the deadline, so a long
        # but healthy decode is never killed.
        timed_out = threading.Event()
        finished = threading.Event()
        last_progress = [time.monotonic()]
        stall_s = DECODE_TIMEOUT_S

        def _watch_stall() -> None:
            while not finished.wait(min(CANCEL_POLL_S, stall_s)):
                if time.monotonic() - last_progress[0] >= stall_s:
                    timed_out.set()
                    proc.kill()
                    return

        threading.Thread(target=_watch_stall, name="audio-scan-stall", daemon=True).start()
        if cancel is not None:
            _watch_cancel(proc, cancel)
        pending = b""
        try:
            while data := proc.stdout.read(chunk_bytes):
                last_progress[0] = time.monotonic()
                data = pending + data
                whole = len(data) - len(data) % frame_bytes
                pending = data[whole:]
                a0 = time.perf_counter()
                if integer_pcm:
                    raw = np.frombuffer(data[:whole], dtype="<i4").reshape(-1, channels)
                    fold.feed(raw.astype(np.float64) / 2**31, raw)
                else:
                    pcm = np.frombuffer(data[:whole], dtype="<f4").reshape(-1, channels)
                    fold.feed(pcm.astype(np.float64), None)
                analysis += time.perf_counter() - a0
        finally:
            finished.set()
            proc.stdout.close()
            proc.wait()
        if cancel is not None and cancel.is_set():
            raise ScanCancelled(str(path))
        if timed_out.is_set():
            raise ScanError(f"decode made no progress for {stall_s} s (stalled)")
        err.seek(0)
        lines = [ln for ln in err.read().decode("utf-8", "replace").splitlines() if ln.strip()]
    return lines, proc.returncode, analysis


def _upsampled(rate: int, brick: bool, cutoff: float | None, t: ScanThresholds) -> bool | None:
    """Hi-res files only: a brick-wall below `upsampled_max_cutoff_hz`."""
    if rate < 88200:
        return None
    return bool(brick and cutoff is not None and cutoff < t.upsampled_max_cutoff_hz)


def _error_count(err_lines: list[str], returncode: int) -> int:
    """ffmpeg's error lines, or 1 for a non-zero exit that printed none."""
    return len(err_lines) + (1 if returncode != 0 and not err_lines else 0)


def scan_file(path: Path, thresholds: ScanThresholds = THRESHOLDS, cancel: threading.Event | None = None) -> ScanFacts:
    """Decode `path` once and measure it. Raises ScanError if nothing decodes,
    ScanCancelled if `cancel` is set mid-decode (the decoder is killed)."""
    t = thresholds
    container = path.suffix.lower()
    lossless = container in LOSSLESS_CONTAINERS
    rate, channels, depth, claimed_kbps, header_s = _header(path)
    if header_s is None:
        # Truncation is header length minus decoded length; without the first there is no
        # measurement, and 0.0 would grade the file as complete (sol review P1, PR #7574).
        raise ScanError("header has no duration, so truncation cannot be measured")
    fold = _Fold(channels, t)
    started = time.perf_counter()
    err_lines, returncode, analysis = _decode(path, channels, lossless, fold, cancel)
    if fold.frames == 0:
        first = err_lines[0] if err_lines else f"ffmpeg exit {returncode}"
        raise ScanError(f"no audio decoded: {first}")
    a0 = time.perf_counter()
    decoded_s = fold.frames / rate
    cutoff, steep, brick = fold.cutoff(rate)
    runs, longest_ms = fold.clipping(rate)
    mono = fold.mono_verdict()
    lead, trail = fold.silence_ends(rate)
    padded = fold.padded_24bit(lossless, depth)
    upsampled = _upsampled(rate, brick, cutoff, t)
    analysis += time.perf_counter() - a0
    total = time.perf_counter() - started
    facts = ScanFacts(
        container=container,
        lossless=lossless,
        sample_rate=rate,
        channels=channels,
        bit_depth=depth,
        claimed_kbps=claimed_kbps,
        header_duration_s=header_s,
        decoded_duration_s=round(decoded_s, 3),
        decode_errors=_error_count(err_lines, returncode),
        first_decode_error=err_lines[0][:200] if err_lines else None,
        truncated_s=round(max(0.0, header_s - decoded_s), 3),
        cutoff_hz=round(cutoff) if cutoff is not None else None,
        cutoff_steep_db=steep,
        brick_wall=brick,
        peak=round(fold.peak, 5),
        clip_runs=runs,
        clip_longest_ms=longest_ms,
        dc_offset=round(float(np.max(np.abs(fold.sum / fold.frames))), 6),
        mono_in_stereo=mono,
        lead_silence_s=round(lead, 2),
        trail_silence_s=round(trail, 2),
        padded_24bit=padded,
        upsampled=upsampled,
        decode_seconds=round(total - analysis, 3),
        analysis_seconds=round(analysis, 3),
    )
    return ScanFacts(**{**asdict(facts), "issues": issues_of(facts, t)})


def issues_of(facts: ScanFacts, t: ScanThresholds = THRESHOLDS) -> list[str]:
    """Issue keys, in the order the hover lists them."""
    checks = (
        ("decode_errors", facts.decode_errors > 0),
        ("truncated", facts.truncated_s > t.truncation_flag_s),
        ("clipping", facts.clip_runs >= t.clip_min_runs_to_flag),
        ("dc_offset", facts.dc_offset > t.dc_offset_flag),
        ("padded_24bit", bool(facts.padded_24bit)),
        ("upsampled", bool(facts.upsampled)),
        ("mono_in_stereo", bool(facts.mono_in_stereo)),
        ("silence_at_start", facts.lead_silence_s > t.lead_silence_flag_s),
        ("silence_at_end", facts.trail_silence_s > t.trail_silence_flag_s),
    )
    return [key for key, hit in checks if hit]


def main(argv: list[str] | None = None) -> int:
    """`python -m apps.shared.audio_scan FILE...`: print facts as JSON lines."""
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("usage: python -m apps.shared.audio_scan FILE...", file=sys.stderr)
        return 2
    rc = 0
    for arg in args:
        try:
            print(json.dumps({"path": arg, **scan_file(Path(arg)).as_dict()}))
        except ScanError as exc:
            print(json.dumps({"path": arg, "error": str(exc)}))
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
