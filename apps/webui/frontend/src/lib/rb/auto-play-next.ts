/**
 * AutoPlay Next: early next-track transition trigger (pin fc60002b81a8).
 *
 * PLAY-11 (issue #3532): transition loops must be downbeat-aligned
 * beat_loop engagements with guaranteed release on completion, abort, and
 * outgoing-deck unload so the fader never strands.
 *
 * Its own file/featurette, deliberately simple and imperfect - no PSSI
 * phrase-kind mapping exists yet (see anlz-types.ts AnlzPhrase), so "the
 * first drop" is APPROXIMATED as a fixed beat count from where the incoming
 * track's bass enters, mirroring the documented fallback the old PR #1063
 * used when phrase data was absent. All engine interaction happens through
 * existing performance commands (beat_loop, eq, seek/play) - this module
 * adds no audio-engine.svelte.ts surface, only pure decision logic plus a
 * thin orchestrator (auto-play-next.svelte.ts) that dispatches them.
 *
 * Requirements:
 *   ✔︎ The outgoing deck's last repetitive (non-progression) 8-beat window
 *     is found by energy-profile self-similarity, not by ear.
 *     [if] two adjacent 8-beat windows have near-identical low/mid/high
 *     energy profiles [then] the later window is offered as the loop
 *   ✔︎ Once armed, that loop plays until the incoming track's bass enters.
 *     [if] the incoming deck's low-band energy crosses the bass threshold
 *     [then] planAutoPlayNextEqDuck returns a ducked LOW knob value exactly once
 *   ✔︎ The loop cuts at the approximate first drop of the incoming track.
 *     [if] dropApproxBeats have elapsed on the incoming deck since bass entry
 *     [then] planAutoPlayNextCut returns that beat's position in ms
 */

import { exactBeatLoopRangeMs } from '$lib/player/transport/loops';
import type { AnlzBeat, AnlzWaveform } from '$lib/rb/anlz-types';

/** Poll-side watchdog: release and disarm if bass never enters within this span. */
export const AUTO_PLAY_NEXT_MAX_LOOP_MS = 120_000;

const AUTO_PLAY_NEXT_BEAT_LOOP_SPAN = 8;
const ONE_SAMPLE_MS_48K = (1 / 48_000) * 1000;

/** Tunables - all config, no hidden defaults baked into the math. */
export interface AutoPlayNextConfig {
	/** 0..1 normalized-correlation floor two 8-beat windows must clear to be
	 * called "the same repetitive loop material". */
	similarityThreshold: number;
	/** 0..1 low-band energy floor that counts as "bass has entered". */
	bassEntryThreshold: number;
	/** Fraction of LOW EQ knob range cut once bass enters (30% per the pin). */
	lowEqDuckFraction: number;
	/** Beats from bass-entry to the approximated drop, absent real phrase data. */
	dropApproxBeats: number;
}

export const DEFAULT_AUTO_PLAY_NEXT_CONFIG: AutoPlayNextConfig = {
	similarityThreshold: 0.92,
	bassEntryThreshold: 0.35,
	lowEqDuckFraction: 0.3,
	dropApproxBeats: 8
};

function _assertBeats(beats: readonly AnlzBeat[]): void {
	if (beats.length === 0) throw new RangeError('beats must be non-empty');
}

/** Mean of a detail array's samples between two beat indices (inclusive-start,
 * exclusive-end), or null when the window is empty/out of range. */
function _windowMean(detail: readonly number[], startIdx: number, endIdx: number): number | null {
	const lo = Math.max(0, startIdx);
	const hi = Math.min(detail.length, endIdx);
	if (hi <= lo) return null;
	let sum = 0;
	for (let i = lo; i < hi; i++) sum += detail[i];
	return sum / (hi - lo);
}

/** detail[] is sampled uniformly across the track; map a beat's [t, nextT)
 * range to detail-array indices by simple proportional scaling. */
function _detailIndexRange(
	detail: readonly number[],
	beats: readonly AnlzBeat[],
	beatIdx: number,
	durationSec: number
): [number, number] {
	const t0 = beats[beatIdx].t;
	const t1 = beatIdx + 1 < beats.length ? beats[beatIdx + 1].t : durationSec;
	const scale = detail.length / Math.max(durationSec, 1e-6);
	return [Math.round(t0 * scale), Math.round(t1 * scale)];
}

/** One window's [low, mid, high] energy-profile vector over `beatSpan` beats
 * starting at `startBeatIdx`, or null if the span runs past the grid/track. */
function _profile(
	waveform: AnlzWaveform,
	beats: readonly AnlzBeat[],
	startBeatIdx: number,
	beatSpan: number,
	durationSec: number
): [number, number, number] | null {
	if (startBeatIdx < 0 || startBeatIdx + beatSpan > beats.length) return null;
	const [lo] = _detailIndexRange(waveform.detail.low, beats, startBeatIdx, durationSec);
	const [, hi] = _detailIndexRange(waveform.detail.low, beats, startBeatIdx + beatSpan - 1, durationSec);
	const low = _windowMean(waveform.detail.low, lo, hi);
	const mid = _windowMean(waveform.detail.mid, lo, hi);
	const high = _windowMean(waveform.detail.high, lo, hi);
	if (low === null || mid === null || high === null) return null;
	return [low, mid, high];
}

function _downbeatAlignedLoopFits(
	beats: readonly AnlzBeat[],
	startBeatIdx: number,
	beatSpan: number
): boolean {
	let snappedStart = startBeatIdx;
	while (snappedStart < beats.length && beats[snappedStart].n !== 1) {
		snappedStart += 1;
	}
	return snappedStart + beatSpan < beats.length && beats[snappedStart].n === 1;
}

/** Cosine similarity of two 3-vectors, 0..1 (profiles are non-negative energy). */
function _similarity(a: readonly [number, number, number], b: readonly [number, number, number]): number {
	const dot = a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
	const magA = Math.hypot(a[0], a[1], a[2]);
	const magB = Math.hypot(b[0], b[1], b[2]);
	if (magA === 0 || magB === 0) return magA === magB ? 1 : 0;
	return dot / (magA * magB);
}

/**
 * Find the last two adjacent, non-overlapping 8-beat (or `beatSpan`-beat)
 * windows on the outgoing deck whose energy profiles are near-identical -
 * an approximation for "repetitive, non-progression" material. Returns the
 * LATER window's beat-index range, or null when no such pair clears
 * `config.similarityThreshold` or the grid/waveform is too short to try.
 */
export function findRepetitiveLoopWindow(
	waveform: AnlzWaveform,
	beats: readonly AnlzBeat[],
	durationSec: number,
	config: AutoPlayNextConfig,
	beatSpan = 8
): { startBeatIdx: number; endBeatIdx: number } | null {
	_assertBeats(beats);
	if (beats.length < beatSpan * 2) return null;
	for (let laterStart = beats.length - beatSpan; laterStart >= beatSpan; laterStart -= beatSpan) {
		const earlierStart = laterStart - beatSpan;
		const later = _profile(waveform, beats, laterStart, beatSpan, durationSec);
		const earlier = _profile(waveform, beats, earlierStart, beatSpan, durationSec);
		if (later === null || earlier === null) continue;
		if (
			_similarity(later, earlier) >= config.similarityThreshold &&
			_downbeatAlignedLoopFits(beats, laterStart, beatSpan)
		) {
			return { startBeatIdx: laterStart, endBeatIdx: laterStart + beatSpan };
		}
	}
	return null;
}

/** Given a repetitive window, return beat_loop args anchored on a PQTZ downbeat,
 * or null when no downbeat-aligned 8-beat span fits the grid and duration. */
export function planAutoPlayNextBeatLoop(
	beats: readonly AnlzBeat[],
	window: { startBeatIdx: number; endBeatIdx: number },
	durationSec: number
): { beats: number; start_ms: number } | null {
	_assertBeats(beats);
	const beatSpan = window.endBeatIdx - window.startBeatIdx;
	if (beatSpan !== AUTO_PLAY_NEXT_BEAT_LOOP_SPAN) return null;
	let snappedStart = window.startBeatIdx;
	while (snappedStart < beats.length && beats[snappedStart].n !== 1) {
		snappedStart += 1;
	}
	if (snappedStart + beatSpan > beats.length) return null;
	if (beats[snappedStart].n !== 1) return null;
	const startMs = beats[snappedStart].t * 1000;
	try {
		const range = exactBeatLoopRangeMs(beats, startMs, beatSpan, startMs);
		if (range.out_ms > durationSec * 1000) return null;
		if (Math.abs(range.in_ms - startMs) > ONE_SAMPLE_MS_48K) return null;
		return { beats: beatSpan, start_ms: range.in_ms };
	} catch {
		return null;
	}
}

/** Beat-index range -> ms range, using the grid's own beat timestamps
 * (exclusive out, matching the beat_loop/loop endpoint convention). */
export function loopWindowToMs(
	beats: readonly AnlzBeat[],
	window: { startBeatIdx: number; endBeatIdx: number },
	durationSec: number
): { in_ms: number; out_ms: number } {
	const inSec = beats[window.startBeatIdx].t;
	const outSec = window.endBeatIdx < beats.length ? beats[window.endBeatIdx].t : durationSec;
	return { in_ms: inSec * 1000, out_ms: outSec * 1000 };
}

/**
 * First ms (at or after `fromMs`) where the incoming deck's low-band energy
 * crosses `config.bassEntryThreshold`, or null if it never does before the
 * waveform ends.
 */
export function bassEntryMs(
	waveform: AnlzWaveform,
	beats: readonly AnlzBeat[],
	durationSec: number,
	fromMs: number,
	config: AutoPlayNextConfig
): number | null {
	_assertBeats(beats);
	const detail = waveform.detail.low;
	const scale = detail.length / Math.max(durationSec, 1e-6);
	const startIdx = Math.max(0, Math.round((fromMs / 1000) * scale));
	for (let i = startIdx; i < detail.length; i++) {
		if (detail[i] >= config.bassEntryThreshold) return (i / scale) * 1000;
	}
	return null;
}

/** The approximated first-drop position: `dropApproxBeats` beats after the
 * beat nearest `bassEnteredMs`, or null once the grid runs out. */
export function approximateDropMs(
	beats: readonly AnlzBeat[],
	bassEnteredMs: number,
	config: AutoPlayNextConfig
): number | null {
	_assertBeats(beats);
	const bassEnteredSec = bassEnteredMs / 1000;
	let nearestIdx = 0;
	let nearestDist = Infinity;
	for (let i = 0; i < beats.length; i++) {
		const dist = Math.abs(beats[i].t - bassEnteredSec);
		if (dist < nearestDist) {
			nearestDist = dist;
			nearestIdx = i;
		}
	}
	const dropIdx = nearestIdx + config.dropApproxBeats;
	if (dropIdx >= beats.length) return null;
	return beats[dropIdx].t * 1000;
}

/** EQ knob unit (0..1, 0.5 = flat) for a `fraction` cut from flat, e.g.
 * lowEqDuckFraction=0.3 -> 0.35 (30% of the way from flat toward min). */
export function duckedLowEqKnob(fraction: number): number {
	if (!Number.isFinite(fraction) || fraction < 0 || fraction > 1) {
		throw new RangeError(`fraction must be within 0..1, got ${fraction}`);
	}
	return 0.5 - 0.5 * fraction;
}
