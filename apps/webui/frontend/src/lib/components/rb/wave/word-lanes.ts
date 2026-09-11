/**
 * Pure lane packing for the main-waveform lyric overlay (build unit:
 * wavestack lyric lanes; specs/karaoke-lyrics-operational-plan.md).
 *
 * No DOM, no canvas, no runes: every function is a plain transform of REAL
 * aligner words (LyricWord from /api/v1/tracks/{id}/lyrics), unit-testable
 * in isolation (tests/unit/word-lanes.test.mjs) and cheap enough to run
 * inside the WaveRow repaint.
 *
 * TEXT WIDTH ESTIMATE: pure code cannot call canvas measureText, so label
 * width is estimated as chars * LANE_CHAR_W_PX. 5.6px/char is a deliberate
 * OVER-estimate of the average glyph advance of a 9px sans-serif (~4.5-5px),
 * so the packer errs toward dropping a word rather than visually colliding
 * two. The painter (render.ts drawLyricLanes) draws at LANE_FONT_PX with the
 * same constant for underlines/backing, keeping estimate and paint coherent.
 *
 * MEMO CONTRACT (documented here, implemented by the caller): lane
 * assignment depends ONLY on word times, text widths and the px-per-second
 * scale - not on the window position. So assignLanes runs once per
 * (track, bucketPxPerS(pxPerS)) and sliceLanes does the per-frame work: one
 * binary search plus a walk over the visible words. Never re-assign per
 * frame in a rAF loop (docs/performance-monitor.md, lyric-lanes table).
 */
import type { LyricWord } from '$lib/api';

/** Canvas font size the painter uses for lane words, CSS px. 11px (was 9):
 * the maintainer's Tue 1 Sep 2026 review called the 9px lanes too hard to read. The
 * canonical wave row (~36px CSS) still fits two 11px lanes under the 10px
 * marker band (lyricLaneYs guards the arithmetic). */
export const LANE_FONT_PX = 11;
/** Estimated average glyph advance at LANE_FONT_PX (see header). Scaled with
 * the font (5.6 * 11/9), kept a deliberate over-estimate. */
export const LANE_CHAR_W_PX = 6.9;
/** Clear pixels required between two labels sharing a lane. */
export const LANE_WORD_GAP_PX = 5;
/** Lanes available over one waveform row (spec: second row when required). */
export const LANE_COUNT = 2;
/** px-per-second quantisation step for the memo key. Pitch moves pxPerS
 * continuously; a 1px/s bucket caps repacks at a handful per fader sweep
 * while keeping lane decisions visually indistinguishable. */
export const PX_PER_S_BUCKET = 1;

/** Marker band at the top of each wave row (cue markers). */
export const MARKER_BAND_PX = 10;
/** Vertical clearance between the two lyric lanes' text boxes. */
export const LANE_V_GAP_PX = 2;

/** Text baselines for the two lyric lanes, tucked under the marker band. */
export function lyricLaneYs(heightCss: number): [number, number] {
	if (heightCss < MARKER_BAND_PX + 2 * LANE_FONT_PX + LANE_V_GAP_PX) {
		throw new RangeError(
			`lyricLaneYs: row height ${heightCss}px cannot hold two ${LANE_FONT_PX}px lanes ` +
				`under the ${MARKER_BAND_PX}px marker band`
		);
	}
	const lane0 = MARKER_BAND_PX + LANE_FONT_PX;
	return [lane0, lane0 + LANE_FONT_PX + LANE_V_GAP_PX];
}

/** One word with a lane decision (window-independent). */
export interface LaneWord {
	/** Source word - `word.idx` is the track-level index used for the
	 * active-word highlight match. */
	word: LyricWord;
	lane: number;
	/** Estimated label width in px at LANE_FONT_PX (see header). */
	widthPx: number;
}

/** Whole-track lane assignment - the memoisable unit. */
export interface AssignedLanes {
	/** Placed words only, time-ordered. Words that fit neither lane at this
	 * scale are dropped whole (never clipped mid-glyph). */
	laneWords: LaneWord[];
	/** Widest label, used by sliceLanes for the left-edge lookback. */
	maxWidthPx: number;
	usedLane1: boolean;
}

/** One placed word for one frame, x in CSS px from the window's left. */
export interface PackedLaneWord extends LaneWord {
	x: number;
}

export interface PackOpts {
	/** Scale used for the LANE DECISION when it differs from the draw scale
	 * (the memo passes the bucketed value; x stays exact). */
	assignPxPerS?: number;
	/** Test hook: override the glyph-advance estimate. */
	charWidthPx?: number;
}

export function estimateLabelWidthPx(text: string, charWidthPx: number = LANE_CHAR_W_PX): number {
	return Math.max(1, text.length) * charWidthPx;
}

/** Quantise pxPerS for the memo key (round to PX_PER_S_BUCKET, floor 1). */
export function bucketPxPerS(pxPerS: number): number {
	if (!Number.isFinite(pxPerS) || pxPerS <= 0) {
		throw new RangeError(`bucketPxPerS: pxPerS must be finite and > 0, got ${pxPerS}`);
	}
	return Math.max(PX_PER_S_BUCKET, Math.round(pxPerS / PX_PER_S_BUCKET) * PX_PER_S_BUCKET);
}

/**
 * Assign every timed word a lane, greedily, for the whole track.
 *
 * Rules (all in absolute px at `pxPerS`, so the result is window-free):
 *   1. words with a null start_s cannot be placed and are skipped - an
 *      unaligned word has no honest position;
 *   2. a word goes to lane 0 when its onset x clears lane 0's cursor
 *      (previous label's right edge + LANE_WORD_GAP_PX), else lane 1 on the
 *      same test, else it is DROPPED whole;
 *   3. cursors only advance, so within a lane no two labels can overlap by
 *      construction (the property the unit test sweeps).
 */
export function assignLanes(
	words: LyricWord[],
	pxPerS: number,
	opts: PackOpts = {}
): AssignedLanes {
	if (!Number.isFinite(pxPerS) || pxPerS <= 0) {
		throw new RangeError(`assignLanes: pxPerS must be finite and > 0, got ${pxPerS}`);
	}
	const charW = opts.charWidthPx ?? LANE_CHAR_W_PX;
	const laneWords: LaneWord[] = [];
	const cursors = new Array<number>(LANE_COUNT).fill(-Infinity);
	let maxWidthPx = 0;
	let usedLane1 = false;
	let prevStart = -Infinity;
	for (const word of words) {
		if (word.start_s == null) continue;
		if (word.start_s < prevStart) {
			throw new Error(
				`assignLanes: words not time-ordered at idx ${word.idx} ` +
					`(start_s ${word.start_s} after ${prevStart}) - upstream contract breach`
			);
		}
		prevStart = word.start_s;
		const x = word.start_s * pxPerS;
		const widthPx = estimateLabelWidthPx(word.word, charW);
		let lane = -1;
		for (let l = 0; l < LANE_COUNT; l++) {
			if (x >= cursors[l]) {
				lane = l;
				break;
			}
		}
		if (lane === -1) continue; // dropped whole - too dense for both lanes
		cursors[lane] = x + widthPx + LANE_WORD_GAP_PX;
		if (widthPx > maxWidthPx) maxWidthPx = widthPx;
		if (lane === 1) usedLane1 = true;
		laneWords.push({ word, lane, widthPx });
	}
	return { laneWords, maxWidthPx, usedLane1 };
}

/** First index in laneWords with start_s >= tSec (binary search; laneWords
 * inherit time order from assignLanes). */
function _firstAtOrAfter(laneWords: LaneWord[], tSec: number): number {
	let lo = 0;
	let hi = laneWords.length;
	while (lo < hi) {
		const mid = (lo + hi) >> 1;
		if ((laneWords[mid].word.start_s as number) < tSec) lo = mid + 1;
		else hi = mid;
	}
	return lo;
}

/**
 * Per-frame window slice: one binary search + a walk over visible words.
 * x is computed at the DRAW scale (exact pxPerSec), which may differ from
 * the assignment scale by up to half a PX_PER_S_BUCKET - a sub-2px drift
 * across the window, accepted so word onsets track the waveform exactly.
 */
export function sliceLanes(
	assigned: AssignedLanes,
	tLeftSec: number,
	pxPerSec: number,
	widthCss: number
): PackedLaneWord[] {
	const { laneWords, maxWidthPx } = assigned;
	const tRight = tLeftSec + widthCss / pxPerSec;
	// A label starting up to maxWidthPx before the left edge can still poke in.
	const from = _firstAtOrAfter(laneWords, tLeftSec - maxWidthPx / pxPerSec);
	const packed: PackedLaneWord[] = [];
	for (let i = from; i < laneWords.length; i++) {
		const lw = laneWords[i];
		const start = lw.word.start_s as number;
		if (start >= tRight) break;
		const x = (start - tLeftSec) * pxPerSec;
		if (x + lw.widthPx <= 0) continue;
		packed.push({ word: lw.word, lane: lw.lane, widthPx: lw.widthPx, x });
	}
	return packed;
}

/**
 * Convenience one-shot: assign + slice. The unit the perf table measures.
 * Callers in a rAF loop must NOT use this - memoise assignLanes per
 * (track, bucketPxPerS) and call sliceLanes per frame (see header).
 */
export function packLyricLanes(
	words: LyricWord[],
	tLeftSec: number,
	pxPerSec: number,
	widthCss: number,
	opts: PackOpts = {}
): PackedLaneWord[] {
	const assigned = assignLanes(words, opts.assignPxPerS ?? pxPerSec, opts);
	return sliceLanes(assigned, tLeftSec, pxPerSec, widthCss);
}

/**
 * Track-level idx of the PLACED word active at tSec under the half-open
 * [start_s, end_s) rule, else null. Operates on placed words on purpose:
 * the highlight can only apply to a word that is drawn. A word with a null
 * end_s has no honest active span and is never active.
 */
export function activeLaneWordIdx(laneWords: LaneWord[], tSec: number): number | null {
	const at = _firstAtOrAfter(laneWords, tSec);
	for (const i of [at, at - 1]) {
		if (i < 0 || i >= laneWords.length) continue;
		const { start_s, end_s, idx } = laneWords[i].word;
		if (start_s == null || end_s == null) continue;
		if (tSec >= start_s && tSec < end_s) return idx;
	}
	return null;
}

/**
 * Per-call cost harness hook (docs/performance-monitor.md, lyric-lanes
 * table): median-free mean microseconds per packLyricLanes call over
 * `iterations` runs, after a small warmup. Pure - the perf unit test calls
 * this and prints the number; nothing in the app path does.
 */
export function packCostMicroseconds(args: {
	words: LyricWord[];
	tLeftSec: number;
	pxPerSec: number;
	widthCss: number;
	iterations?: number;
}): number {
	const { words, tLeftSec, pxPerSec, widthCss } = args;
	const iterations = args.iterations ?? 500;
	if (iterations < 1) throw new RangeError(`packCostMicroseconds: iterations >= 1 required`);
	for (let i = 0; i < 20; i++) packLyricLanes(words, tLeftSec, pxPerSec, widthCss);
	const t0 = globalThis.performance.now();
	for (let i = 0; i < iterations; i++) packLyricLanes(words, tLeftSec, pxPerSec, widthCss);
	const t1 = globalThis.performance.now();
	return ((t1 - t0) * 1000) / iterations;
}
