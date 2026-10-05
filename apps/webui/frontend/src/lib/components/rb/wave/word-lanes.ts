/**
 * Pure lane packing for the main-waveform word overlay (build unit:
 * wavestack word lanes; specs/karaoke-lyrics-operational-plan.md).
 *
 * No DOM, no canvas, no runes: every function is a plain transform of REAL
 * aligner words (a structural subset of the LyricWord shape served at
 * /api/v1/tracks/{id}/lyrics, declared locally below as LaneInputWord so
 * this module imports nothing), unit-testable in isolation
 * (tests/unit/word-lanes.test.mjs) and cheap enough to run inside the
 * WaveRow repaint.
 *
 * TEXT WIDTH ESTIMATE: pure code cannot call canvas measureText, so label
 * width is estimated as chars * LANE_CHAR_W_PX. 5.6px/char is a deliberate
 * OVER-estimate of the average glyph advance of a 9px sans-serif (~4.5-5px),
 * so the packer errs toward dropping a word rather than visually colliding
 * two. The painter (WordLane.svelte) draws at LANE_FONT_PX with the same
 * constant for underlines/backing, keeping estimate and paint coherent; the
 * painter is DOM now, not canvas.
 *
 * MEMO CONTRACT (documented here, implemented by the caller): lane
 * assignment depends ONLY on word times, text widths and the px-per-second
 * scale - not on the window position. So assignLanes runs once per
 * (track, bucketPxPerS(pxPerS)) and sliceLanes does the per-frame work: one
 * binary search plus a walk over the visible words. Never re-assign per
 * frame in a rAF loop (docs/perf/performance-register.md, word-lanes table).
 */

/** One aligner word as consumed by the packer - a structural subset of the
 * LyricWord shape served at /api/v1/tracks/{id}/lyrics. Declared locally
 * (not imported from $lib/api) so this module stays dependency-free. */
export interface LaneInputWord {
	idx: number;
	word: string;
	start_s: number | null;
	end_s: number | null;
}

/** Canvas font size the painter uses for lane words, CSS px. 11px (was 9):
 * the maintainer's Tue 1 Sep 2026 review called the 9px lanes too hard to read. The
 * canonical wave row (~36px CSS) still fits two 11px lanes under the 10px
 * marker band (the DOM layout in WordLane.svelte guards the arithmetic). */
export const LANE_FONT_PX = 11;
/** Estimated average glyph advance at LANE_FONT_PX (see header). Scaled with
 * the font (5.6 * 11/9), kept a deliberate over-estimate. */
export const LANE_CHAR_W_PX = 6.9;
/** Clear pixels required between two labels sharing a lane. */
export const LANE_WORD_GAP_PX = 5;
/** Lanes over one waveform row; placed words alternate between them. */
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
	word: LaneInputWord;
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
 *   2. placed words ALTERNATE lanes (first word lane 0, then 1, 0, ...), so
 *      neighbors never share a row (design record: re-skinning
 *      design-widgets, option G); a word whose turn-lane cursor (previous
 *      label's right edge + LANE_WORD_GAP_PX) is still ahead of its onset x
 *      is DROPPED whole and the next word takes that lane instead;
 *   3. cursors only advance, so within a lane no two labels can overlap by
 *      construction (the property the unit test sweeps).
 */
export function assignLanes(
	words: LaneInputWord[],
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
	let lastLane = -1;
	for (const word of words) {
		if (word.start_s === null) continue;
		if (word.start_s < prevStart) {
			throw new Error(
				`assignLanes: words not time-ordered at idx ${word.idx} ` +
					`(start_s ${word.start_s} after ${prevStart}) - upstream contract breach`
			);
		}
		prevStart = word.start_s;
		const x = word.start_s * pxPerS;
		const widthPx = estimateLabelWidthPx(word.word, charW);
		const lane = (lastLane + 1) % LANE_COUNT;
		if (x < cursors[lane]) continue; // dropped whole - its turn lane is still occupied
		cursors[lane] = x + widthPx + LANE_WORD_GAP_PX;
		lastLane = lane;
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
 * Waveform window geometry for one frame: the window shown is
 * `windowSeconds * pitch` seconds of track time centred on the playhead
 * (the same semantic the main's lyrics-lane.ts lyricLanePositionPercent
 * encodes: `50 + (t - pos) / (windowSeconds * pitch * 1000) * 100`).
 * Returns the left edge of that window in track seconds and the resulting
 * px-per-second scale, ready to feed sliceLanes/packWordLanes.
 */
export function laneWindow(args: {
	positionMs: number;
	pitch: number;
	widthCss: number;
	windowSeconds: number;
}): { tLeftSec: number; pxPerS: number } {
	const { positionMs, pitch, widthCss, windowSeconds } = args;
	if (
		!Number.isFinite(positionMs) ||
		!Number.isFinite(pitch) ||
		!Number.isFinite(widthCss) ||
		!Number.isFinite(windowSeconds)
	) {
		throw new RangeError(`laneWindow: all inputs must be finite, got ${JSON.stringify(args)}`);
	}
	if (pitch <= 0 || widthCss <= 0 || windowSeconds <= 0) {
		throw new RangeError(
			`laneWindow: pitch, widthCss and windowSeconds must be > 0, got ${JSON.stringify(args)}`
		);
	}
	const windowSpanSec = windowSeconds * pitch;
	return {
		tLeftSec: positionMs / 1000 - windowSpanSec / 2,
		pxPerS: widthCss / windowSpanSec
	};
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
		if (start_s === null || end_s === null) continue;
		if (tSec >= start_s && tSec < end_s) return idx;
	}
	return null;
}
