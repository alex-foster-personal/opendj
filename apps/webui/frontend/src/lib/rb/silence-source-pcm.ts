/**
 * Source PCM at the transport playhead, for the master silence watchdog (issue
 * #4030). Master-bus RMS can be near zero during a track's own silent intro or
 * outro; comparing against the held decode buffer distinguishes that from a
 * real dropout (loud source, quiet master).
 *
 * The mixer is the other honest reason for a quiet master: a deck pre-cued in
 * the headphones with its channel fader down, or parked on the far side of the
 * crossfader, is playing and claims live while nothing of it reaches the master
 * bus. So each deck's source RMS is scaled by its linear gain to the master
 * tap before it is compared with the floor.
 *
 * A ready stem bundle is the third (AUDIOLIVE-17): its PCM is transferred to
 * the worklet (PERF-STEMDEC-05), so a soloed part that is silent in this
 * passage cannot be read at the playhead, and the mix buffer says the deck is
 * loud. Each part keeps a block envelope taken before the transfer, and a
 * stem deck's expected level is its path gain x the sum of part gain x part
 * window RMS bound. The bound is built from block mean squares, not block
 * peaks: a peak can sit far above the RMS the master meter reads (review
 * thread r4268103313), which would turn a genuinely quiet soloed passage into
 * a false AUDIO CUT.
 */

import { SILENCE_RMS_FLOOR } from "$lib/rb/silence-watchdog";

/** Linear gain below which a deck's path to the master bus is closed, -80 dBFS
 * (same floor as `SILENCE_GAIN_EPSILON` in `master-election.ts`). */
const CLOSED_PATH_GAIN = 1e-4;

export interface SilenceSourceDeckSnap {
  claims_live: boolean;
  buffer: AudioBuffer | null;
  /** Track timeline seconds; same basis as deck transport position. */
  position_sec: number;
  /**
   * Linear gain from this deck's source to the master analyser tap (trim x
   * fader x crossfader x master, 0 when every stem part is gained to zero).
   * Trim can boost above 1, so an absent reading is unknown, not "open", and
   * never explains silence.
   */
  master_path_gain?: number;
  /**
   * Present when the deck's signal path is a stem graph: one entry per part,
   * its linear part gain (mute/solo/knob) and its pre-transfer peak envelope.
   * When present, the parts are the source and `buffer` is not read.
   */
  stem_parts?: readonly SilenceStemPartSnap[];
}

export interface SilenceStemPartSnap {
  gain: number;
  /** Null when the part has no envelope; an open part without one never explains silence. */
  envelope: PeakEnvelope | null;
}

/** Per fixed block, across every channel: the largest absolute sample and the
 * mean square. */
export interface PeakEnvelope {
  block_sec: number;
  duration_sec: number;
  peaks: Float32Array;
  mean_squares: Float32Array;
}

/** 100 ms: 3,600 floats for a 6 minute part, and finer than the 0.5 s gate window. */
export const PEAK_ENVELOPE_BLOCK_SEC = 0.1;

const DEFAULT_WINDOW_SEC = 0.5;

/** RMS 0..1 of mixed channels over a short window from position_sec. NaN when
 * the window starts outside the decoded buffer, and non-finite when the PCM
 * holds NaN or Infinity: an invalid measurement is returned as such, never as
 * silence, so the caller can refuse to read it as content. */
export function rmsAtPlayhead(
  buffer: AudioBuffer,
  position_sec: number,
  window_sec: number = DEFAULT_WINDOW_SEC,
): number {
  if (
    !Number.isFinite(position_sec) ||
    !Number.isFinite(window_sec) ||
    window_sec <= 0
  ) {
    throw new RangeError(
      `position_sec and window_sec must be finite with window_sec > 0, got ${position_sec}, ${window_sec}`,
    );
  }
  const sampleRate = buffer.sampleRate;
  const start = Math.floor(position_sec * sampleRate);
  if (start < 0 || start >= buffer.length) return Number.NaN;
  const windowSamples = Math.max(1, Math.floor(window_sec * sampleRate));
  const count = Math.min(windowSamples, buffer.length - start);
  if (count <= 0) return 0;
  let sum = 0;
  let samples = 0;
  for (let ch = 0; ch < buffer.numberOfChannels; ch++) {
    const data = buffer.getChannelData(ch);
    for (let i = 0; i < count; i++) {
      const v = data[start + i] ?? 0;
      sum += v * v;
      samples++;
    }
  }
  return Math.sqrt(sum / samples);
}

/** Block envelope (peak and mean square) of a decoded buffer. One pass, run
 * before the PCM is transferred away; `rmsBoundAtPlayhead` reads it as an
 * upper bound on window RMS, so it can over-predict sound but never
 * under-predict it. */
export function peakEnvelope(
  buffer: AudioBuffer,
  block_sec: number = PEAK_ENVELOPE_BLOCK_SEC,
): PeakEnvelope {
  const scan = _envelopeScan(buffer, block_sec);
  _scanBlocks(buffer, scan.blockSamples, scan.envelope, 0, scan.peaks.length);
  return scan.envelope;
}

/** Longest slice `peakEnvelopeYielding` scans before giving the main thread
 * back: well inside the 16 ms Class A visual budget (LATENCY-01). */
export const PEAK_ENVELOPE_SLICE_MS = 4;

/**
 * `peakEnvelope`, but in slices of at most ~PEAK_ENVELOPE_SLICE_MS that yield
 * a macrotask between them. A live stem upgrade scans every part on the main
 * thread (~28 ms per six-minute part, (private path)), so
 * one synchronous pass per part would hold play/cue/EQ/fader dispatch past
 * its budget (P1 thread r4279728753). Same result as `peakEnvelope`.
 */
export async function peakEnvelopeYielding(
  buffer: AudioBuffer,
  block_sec: number = PEAK_ENVELOPE_BLOCK_SEC,
  slice_ms: number = PEAK_ENVELOPE_SLICE_MS,
): Promise<PeakEnvelope> {
  const scan = _envelopeScan(buffer, block_sec);
  const total = scan.peaks.length;
  let b = 0;
  // Blocks per slice, re-sized from the measured rate so a slice stays near slice_ms.
  let step = 16;
  while (b < total) {
    const started = performance.now();
    const end = Math.min(total, b + step);
    _scanBlocks(buffer, scan.blockSamples, scan.envelope, b, end);
    const took = performance.now() - started;
    step = Math.max(
      1,
      Math.floor(((end - b) * slice_ms) / Math.max(took, 0.05)),
    );
    b = end;
    if (b < total) await new Promise<void>((resolve) => setTimeout(resolve, 0));
  }
  return scan.envelope;
}

function _envelopeScan(buffer: AudioBuffer, block_sec: number) {
  if (!Number.isFinite(block_sec) || block_sec <= 0) {
    throw new RangeError(`block_sec must be finite and > 0, got ${block_sec}`);
  }
  const blockSamples = Math.max(1, Math.round(block_sec * buffer.sampleRate));
  const blocks = Math.ceil(buffer.length / blockSamples);
  const peaks = new Float32Array(blocks);
  const envelope: PeakEnvelope = {
    block_sec: blockSamples / buffer.sampleRate,
    duration_sec: buffer.length / buffer.sampleRate,
    peaks,
    mean_squares: new Float32Array(blocks),
  };
  return { blockSamples, peaks, envelope };
}

/** Fold blocks [first, last) of every channel into the envelope. */
function _scanBlocks(
  buffer: AudioBuffer,
  blockSamples: number,
  envelope: PeakEnvelope,
  first: number,
  last: number,
): void {
  const { peaks, mean_squares } = envelope;
  const channels = buffer.numberOfChannels;
  for (let ch = 0; ch < channels; ch++) {
    const data = buffer.getChannelData(ch);
    for (let b = first; b < last; b++) {
      let peak = peaks[b];
      let sq = 0;
      const begin = b * blockSamples;
      const end = Math.min(buffer.length, (b + 1) * blockSamples);
      for (let i = begin; i < end; i++) {
        const v = data[i];
        sq += v * v; // NaN or Infinity carries through, like the peak
        const mag = v < 0 ? -v : v;
        // An invalid sample poisons its block rather than vanishing, so the
        // gate refuses to read it as silence. `mag !== mag` takes a NaN in,
        // and once peak is NaN no `mag > peak` can take it back out.
        if (mag > peak || mag !== mag) peak = mag;
      }
      peaks[b] = peak;
      mean_squares[b] += sq / (channels * Math.max(1, end - begin));
    }
  }
}

/** Max peak over the blocks a window from position_sec overlaps. NaN when the
 * playhead is outside the envelope, mirroring `rmsAtPlayhead`. */
export function peakAtPlayhead(
  envelope: PeakEnvelope,
  position_sec: number,
  window_sec: number = DEFAULT_WINDOW_SEC,
): number {
  if (
    !Number.isFinite(position_sec) ||
    position_sec < 0 ||
    position_sec >= envelope.duration_sec
  ) {
    return Number.NaN;
  }
  const first = Math.floor(position_sec / envelope.block_sec);
  const last = Math.min(
    envelope.peaks.length - 1,
    Math.ceil((position_sec + window_sec) / envelope.block_sec) - 1,
  );
  let peak = 0;
  for (let b = first; b <= Math.max(first, last); b++) {
    const v = envelope.peaks[b];
    if (v > peak || v !== v) peak = v; // a NaN block stays NaN, as in peakEnvelope
  }
  return peak;
}

/**
 * Upper bound on `rmsAtPlayhead` of the part's PCM for the same window, from
 * block mean squares: the window lies inside the blocks it overlaps, so its
 * energy is at most theirs, and dividing by the window's own length (clipped
 * at the decoded end, as `rmsAtPlayhead` clips) keeps it a bound. It reads
 * exact on a block-aligned window and otherwise adds at most the energy of the
 * one partly covered block at each edge, where a block peak can over-read RMS
 * by 18 dB or more. NaN outside the envelope or over a non-finite block.
 */
export function rmsBoundAtPlayhead(
  envelope: PeakEnvelope,
  position_sec: number,
  window_sec: number = DEFAULT_WINDOW_SEC,
): number {
  if (
    !Number.isFinite(position_sec) ||
    position_sec < 0 ||
    position_sec >= envelope.duration_sec
  ) {
    return Number.NaN;
  }
  const blocks = envelope.mean_squares.length;
  // A nanosecond of slack, far under one sample, so float error at a block
  // edge (1.2 / 0.1 = 11.999...) does not pull in a neighbouring block.
  const first = Math.floor(position_sec / envelope.block_sec + 1e-9);
  const last = Math.min(
    blocks - 1,
    Math.ceil((position_sec + window_sec) / envelope.block_sec - 1e-9) - 1,
  );
  let energy = 0;
  for (let b = first; b <= Math.max(first, last); b++) {
    const blockEnd = Math.min(
      envelope.duration_sec,
      (b + 1) * envelope.block_sec,
    );
    energy += envelope.mean_squares[b] * (blockEnd - b * envelope.block_sec);
  }
  const span = Math.min(window_sec, envelope.duration_sec - position_sec);
  return Math.sqrt(energy / span);
}

/** Sum of part gain x part window RMS bound at the playhead; NaN when any open part cannot be measured. */
function _stemDeckPeak(
  parts: readonly SilenceStemPartSnap[],
  position_sec: number,
  window_sec: number,
): number {
  if (parts.length === 0) return Number.NaN;
  let sum = 0;
  for (const part of parts) {
    if (!Number.isFinite(part.gain) || part.gain < 0) return Number.NaN;
    if (part.gain < CLOSED_PATH_GAIN) continue;
    if (part.envelope === null) return Number.NaN;
    sum +=
      part.gain * rmsBoundAtPlayhead(part.envelope, position_sec, window_sec);
  }
  return sum;
}

/**
 * True when the claimed-live decks' expected contribution to the master bus is
 * below floor: the sum over those decks of master-path gain x playhead RMS. The
 * sum bounds the RMS of the mixed signal from above, so a quiet master next to
 * a quiet expectation is content, never a cut. A deck whose path is closed
 * (fader down, crossfader cut, master at zero, every stem muted) contributes 0
 * without its PCM being read. Everything this cannot measure fails safe toward
 * reporting a real dropout, never toward hiding one:
 * - an absent, non-finite or negative path gain does not explain silence;
 * - non-finite PCM in the window does not explain silence;
 * - an open-path claimed-live deck with no buffer does not explain silence;
 * - a non-finite or negative playhead does not explain silence (and must not throw: a throw
 *   here would skip the fold in `noteMasterSilence`, so the watchdog would stop
 *   counting for as long as the bad position lasted);
 * - a playhead at or past the decoded end does not explain silence: a deck still
 *   claiming live there is a stuck transport, which is exactly what the
 *   watchdog's honest stop exists to catch.
 * - a stem deck with no parts, an unreadable part gain, an open part with no
 *   envelope, or a playhead outside a part's envelope does not explain silence.
 */
export function claimedLiveSourceIsSilent(
  decks: readonly SilenceSourceDeckSnap[],
  floor: number = SILENCE_RMS_FLOOR,
  window_sec: number = DEFAULT_WINDOW_SEC,
): boolean {
  const live = decks.filter((deck) => deck.claims_live);
  if (live.length === 0) return false;
  let expected = 0;
  for (const deck of live) {
    const gain = deck.master_path_gain;
    if (gain === undefined || !Number.isFinite(gain) || gain < 0) return false;
    if (gain < CLOSED_PATH_GAIN) continue;
    if (deck.stem_parts !== undefined) {
      const level = _stemDeckPeak(
        deck.stem_parts,
        deck.position_sec,
        window_sec,
      );
      if (!Number.isFinite(level)) return false;
      expected += gain * level;
      if (expected >= floor) return false;
      continue;
    }
    if (deck.buffer === null) return false;
    if (!Number.isFinite(deck.position_sec) || deck.position_sec < 0)
      return false;
    if (deck.position_sec * deck.buffer.sampleRate >= deck.buffer.length)
      return false;
    const rms = rmsAtPlayhead(deck.buffer, deck.position_sec, window_sec);
    if (!Number.isFinite(rms)) return false;
    expected += gain * rms;
    if (expected >= floor) return false;
  }
  return true;
}
