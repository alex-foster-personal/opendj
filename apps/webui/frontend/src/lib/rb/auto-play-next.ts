/**
 * AutoPlay Next: early next-track transition trigger (pin fc60002b81a8).
 *
 * PLAY-11 (issue #3532): transition loops must be downbeat-aligned
 * beat_loop engagements with guaranteed release on completion, abort, and
 * outgoing-deck unload so the fader never strands.
 *
 * Its own file/featurette, deliberately simple and imperfect - no PSSI
 * incoming drop timing still uses a bass-entry approximation when phrase
 * moments are absent; mix-out / mix-in bar choice is in auto-play-windows.ts
 * when both decks carry phrase analysis (#11180). All engine interaction happens through
 * existing performance commands (beat_loop, eq, seek/play) - this module
 * adds no audio-engine.svelte.ts surface, only pure decision logic plus a
 * thin orchestrator (auto-play-next.svelte.ts) that dispatches them.
 *
 * Requirements:
 *   ✔︎ The outgoing deck loops about 16 bars before the end (B-16, B = end
 *     of the beat grid), not on the last bars. DECKGAP-7691g, pin 9f05ca7e3f6f.
 *     [if] a nearby downbeat has near-identical low/mid/high energy on the
 *     4 beats before it and the 4 beats after it [then] that downbeat starts
 *     the 8-beat loop
 *     [else] the loop starts at B-16 anyway
 *     Grids shorter than 16 bars keep the last repetitive 8-beat window.
 *   ✔︎ Once armed, that loop plays until the incoming track's bass enters.
 *     [if] the incoming deck's low-band energy crosses the bass threshold
 *     [then] planAutoPlayNextEqDuck returns a ducked LOW knob value exactly once
 *   ✔︎ The loop cuts at the approximate first drop of the incoming track.
 *     [if] dropApproxBeats have elapsed on the incoming deck since bass entry
 *     [then] planAutoPlayNextCut returns that beat's position in ms
 */

import { exactBeatLoopRangeMs } from '$lib/player/transport/loops';
import type { AnlzBeat, AnlzPhrase, AnlzVocals, AnlzWaveform } from '$lib/rb/anlz-types';
import {
	beatIndexForBar,
	hasPhraseAnalysis,
	mixWindowsForTrack,
	type AutoPlayMixPair,
	type AutoPlayMixTrackInput
} from '$lib/rb/auto-play-windows';

/** Poll-side watchdog: release and disarm if bass never enters within this span. */
export const AUTO_PLAY_NEXT_MAX_LOOP_MS = 120_000;

const AUTO_PLAY_NEXT_BEAT_LOOP_SPAN = 8;
export { AUTO_PLAY_NEXT_BEAT_LOOP_SPAN };
const ONE_SAMPLE_MS_48K = (1 / 48_000) * 1000;
/** 4/4 bar. B-16 is 16 of these before the end of the grid. */
const BEATS_PER_BAR = 4;
const LOOP_BARS_BEFORE_END = 16;
/** Beats compared on each side of a candidate loop start. */
const SEAM_BEATS = 4;
/** How far before B-16 a seamless bar may sit. */
const SEAM_SEARCH_EARLIER_BARS = 32;
/** How far after B-16 a seamless bar may sit (stays off the tail). */
const SEAM_SEARCH_LATER_BARS = 4;

/** Tunables - all config, no hidden defaults baked into the math. */
export interface AutoPlayNextConfig {
	/** 0..1 cosine-similarity floor two energy profiles must clear to count
	 * as the same bar (4 beats before vs after a downbeat, or two 8-beat
	 * windows on a grid shorter than 16 bars). */
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
	const [, hi] = _detailIndexRange(
		waveform.detail.low,
		beats,
		startBeatIdx + beatSpan - 1,
		durationSec
	);
	const low = _windowMean(waveform.detail.low, lo, hi);
	const mid = _windowMean(waveform.detail.mid, lo, hi);
	const high = _windowMean(waveform.detail.high, lo, hi);
	if (low === null || mid === null || high === null) return null;
	return [low, mid, high];
}

/** Cosine similarity of two 3-vectors, 0..1 (profiles are non-negative energy). */
function _similarity(
	a: readonly [number, number, number],
	b: readonly [number, number, number]
): number {
	const dot = a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
	const magA = Math.hypot(a[0], a[1], a[2]);
	const magB = Math.hypot(b[0], b[1], b[2]);
	if (magA === 0 || magB === 0) return magA === magB ? 1 : 0;
	return dot / (magA * magB);
}

function _snapDownbeatAtOrBefore(beats: readonly AnlzBeat[], index: number): number | null {
	if (index < 0) return null;
	let idx = Math.min(index, beats.length - 1);
	while (idx >= 0 && beats[idx].n !== 1) idx -= 1;
	return idx >= 0 ? idx : null;
}

/** Downbeat where 16 bars remain, or null when the grid is shorter than that. */
function _bMinus16LoopStart(beats: readonly AnlzBeat[], beatSpan: number): number | null {
	const raw = beats.length - LOOP_BARS_BEFORE_END * BEATS_PER_BAR;
	const start = _snapDownbeatAtOrBefore(beats, raw);
	if (start === null) return null;
	if (!_downbeatAlignedLoopFits(beats, start, beatSpan)) return null;
	return start;
}

/**
 * Grids shorter than 16 bars: last two adjacent 8-beat windows whose energy
 * profiles match. Returns the later window, or null when none clears the
 * threshold.
 */
function _findLastRepetitiveWindow(
	waveform: AnlzWaveform,
	beats: readonly AnlzBeat[],
	durationSec: number,
	config: AutoPlayNextConfig,
	beatSpan: number
): { startBeatIdx: number; endBeatIdx: number } | null {
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

/**
 * Pin 9f05ca7e3f6f: 8-beat loop near B-16. Prefer a downbeat whose 4 beats
 * before and 4 beats after have near-identical energy; otherwise start the
 * loop at B-16 itself. Null only when the grid has no such downbeat.
 */
function _findLoopAroundBMinus16(
	waveform: AnlzWaveform,
	beats: readonly AnlzBeat[],
	durationSec: number,
	config: AutoPlayNextConfig,
	beatSpan: number
): { startBeatIdx: number; endBeatIdx: number } | null {
	const target = _bMinus16LoopStart(beats, beatSpan);
	if (target === null) return null;
	const earliest = Math.max(SEAM_BEATS, target - SEAM_SEARCH_EARLIER_BARS * BEATS_PER_BAR);
	const latest = target + SEAM_SEARCH_LATER_BARS * BEATS_PER_BAR;
	let best: { start: number; dist: number; sim: number } | null = null;
	for (let start = earliest; start <= latest; start += 1) {
		if (beats[start] === undefined || beats[start].n !== 1) continue;
		if (!_downbeatAlignedLoopFits(beats, start, beatSpan)) continue;
		const before = _profile(waveform, beats, start - SEAM_BEATS, SEAM_BEATS, durationSec);
		const after = _profile(waveform, beats, start, SEAM_BEATS, durationSec);
		if (before === null || after === null) continue;
		const sim = _similarity(before, after);
		if (sim < config.similarityThreshold) continue;
		const dist = Math.abs(start - target);
		if (
			best === null ||
			dist < best.dist ||
			(dist === best.dist && (sim > best.sim || (sim === best.sim && start < best.start)))
		) {
			best = { start, dist, sim };
		}
	}
	const chosen = best === null ? target : best.start;
	return { startBeatIdx: chosen, endBeatIdx: chosen + beatSpan };
}

/**
 * Outgoing-deck loop window for AutoPlay Next. On a grid of at least 16 bars
 * this is an 8-beat span near B-16 (see `_findLoopAroundBMinus16`). Shorter
 * grids use the last repetitive 8-beat window. Null when nothing fits.
 */
export function autoPlayMixTrackInput(
	beats: readonly AnlzBeat[],
	durationSec: number,
	phrases: readonly AnlzPhrase[],
	phrasesSource?: string,
	vocals?: AnlzVocals | null
): AutoPlayMixTrackInput {
	const input: AutoPlayMixTrackInput = {
		beats,
		duration_sec: durationSec,
		phrases
	};
	if (phrasesSource !== undefined) input.phrases_source = phrasesSource;
	if (vocals !== undefined) input.vocals = vocals;
	return input;
}

export type { AutoPlayMixPair, AutoPlayMixTrackInput };

/**
 * Ranked phrase windows for one deck, or [] when phrase analysis is absent
 * (callers keep the B-16 / 8-beat loop fallback).
 */
export function autoPlayMixWindows(input: AutoPlayMixTrackInput, role: 'out' | 'in', limit = 5) {
	return mixWindowsForTrack(input, role, limit);
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

/**
 * Prefer a phrase-ranked mix-out bar when analysis exists; otherwise the
 * DECKGAP-7691g B-16 / repetitive 8-beat window.
 */
export function findAutoPlayNextLoopWindow(
	waveform: AnlzWaveform,
	beats: readonly AnlzBeat[],
	durationSec: number,
	config: AutoPlayNextConfig,
	mixInput?: AutoPlayMixTrackInput | null,
	beatSpan = AUTO_PLAY_NEXT_BEAT_LOOP_SPAN
): { startBeatIdx: number; endBeatIdx: number; mix_out_bar: number | null } | null {
	_assertBeats(beats);
	if (mixInput !== null && mixInput !== undefined && hasPhraseAnalysis(mixInput)) {
		const ranked = mixWindowsForTrack(mixInput, 'out', 1);
		const best = ranked[0];
		if (best !== undefined) {
			const startBeatIdx = beatIndexForBar(beats, best.start_bar);
			if (startBeatIdx !== null && _downbeatAlignedLoopFits(beats, startBeatIdx, beatSpan)) {
				return { startBeatIdx, endBeatIdx: startBeatIdx + beatSpan, mix_out_bar: best.start_bar };
			}
		}
	}
	const fallback = findRepetitiveLoopWindow(waveform, beats, durationSec, config, beatSpan);
	if (fallback === null) return null;
	return { ...fallback, mix_out_bar: null };
}

export function findRepetitiveLoopWindow(
	waveform: AnlzWaveform,
	beats: readonly AnlzBeat[],
	durationSec: number,
	config: AutoPlayNextConfig,
	beatSpan = 8
): { startBeatIdx: number; endBeatIdx: number } | null {
	_assertBeats(beats);
	if (beats.length < beatSpan * 2) return null;
	const aroundEnd = _findLoopAroundBMinus16(waveform, beats, durationSec, config, beatSpan);
	if (aroundEnd !== null) return aroundEnd;
	return _findLastRepetitiveWindow(waveform, beats, durationSec, config, beatSpan);
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
