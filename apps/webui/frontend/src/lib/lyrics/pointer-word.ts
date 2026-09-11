/**
 * Pointer x -> track time -> the lyric word at/near that time. Pure maths,
 * no DOM, no clock - the ONE place the scrub-hover surfaces (PreviewStrip,
 * StripWaveform) resolve a pointer against word timings.
 *
 * Ported from branch af--lyrics-scrub-hover (pointer-word.ts) and adapted to
 * the $lib/api LyricWord shape (start_s/end_s nullable, idx-ordered).
 *
 * CLOCK CONTRACT: word timings are immutable source-track seconds; intervals
 * are HALF-OPEN `[start_s, end_s)` so start is INCLUSIVE and end is
 * EXCLUSIVE, and an inter-word gap is a REAL state with no active word.
 *
 * Two invariants held here, because sibling builds each broke one by
 * implementing the rule twice:
 *
 *  1. WHILE A WORD IS ACTIVE THERE IS NO GAP. `gapToNextS` and `sincePrevS`
 *     are null for kind 'inside'. A 29s sustained 'ooh' whose successor
 *     starts 0.04s later must never read as 25s of instrumental.
 *  2. EXACT BOUNDARIES. `t === start_s` is INSIDE; `t === end_s` is NOT.
 *     A binary search that only inspects the predecessor of its insertion
 *     point reads the first sample of every word as a gap.
 *
 * PERFORMANCE: `activeWordAt` is BINARY SEARCH over start_s (never a linear
 * scan per mousemove). Measured cost lives in docs/performance-monitor.md
 * ("Scrub-hover lyric readout" table); the unit perf case re-measures it.
 *
 * Requirements (mini-PRD):
 * - ✔︎ ✅ 🎯 resolvePointerWord is the only place either invariant is decided.
 * - ✔︎ ✅ 🎯 Half-open intervals, both insertion-point neighbours inspected.
 * - ✔︎ ✅ 🎯 Untimed words (null start_s/end_s) are excluded from the time
 *   index but preserved in display order for the readout.
 * - ✔︎ ✅ 🎯 Never invents a time or a word: no timed words -> null index.
 *
 * Acceptance tests (tests/unit/pointer-word.test.mjs):
 * - [if] t is exactly a word's start_s [then] kind 'inside'.
 * - [if] t is exactly a word's end_s [then ⛔️] kind is 'inside'.
 * - [if] t is mid-sustain [then ⛔️] gapToNextS is non-null.
 * - [if] 10k lookups on 500 words [then] per-call cost prints in us.
 */
import type { LyricWord } from '$lib/api';

// -------------------------------------------------------------- tuning

/** Adjacency band in on-screen PIXELS: "just before/after" means the same
 * pointer distance at every zoom. Converted to seconds per surface. */
export const SCRUB_NEAR_PX = 12;
/** Floor so an absurd zoom-in never demands sub-frame pointer precision. */
export const SCRUB_MIN_NEAR_S = 0.25;
/** Cap so a 165px library strip over a 5-minute track does not call half
 * the song "just before" a word. */
export const SCRUB_MAX_NEAR_S = 4;

/** The adjacency band for a surface, from its seconds-per-pixel scale. */
export function nearSecondsForScale(secondsPerPixel: number): number {
	if (!Number.isFinite(secondsPerPixel) || secondsPerPixel <= 0) {
		throw new RangeError(`nearSecondsForScale: bad seconds-per-pixel ${secondsPerPixel}`);
	}
	return Math.min(SCRUB_MAX_NEAR_S, Math.max(SCRUB_MIN_NEAR_S, SCRUB_NEAR_PX * secondsPerPixel));
}

// -------------------------------------------------------------- indexing

export interface WordIndex {
	/** The full word list as served, display (idx) order, untimed included. */
	words: LyricWord[];
	/** timed[k] = position in `words` of the k-th timed word, start_s order. */
	timed: Int32Array;
	startsS: Float64Array;
	endsS: Float64Array;
	/** Prefix max of endsS: the overlap guard that bounds the backward walk. */
	maxEndUpToS: Float64Array;
}

/**
 * Index a track's words once per load. Returns null when NO word carries
 * timings (the honest "nothing to scrub" state - callers render no readout,
 * never a fabricated one). Throws on an empty word list: the caller should
 * have treated that as no lyric data before ever building an index.
 */
export function indexLyricWords(words: LyricWord[]): WordIndex | null {
	if (words.length === 0) throw new RangeError('indexLyricWords: empty word list');
	const timedPositions: number[] = [];
	for (let i = 0; i < words.length; i++) {
		const w = words[i];
		if (w.start_s !== null && w.end_s !== null) timedPositions.push(i);
	}
	if (timedPositions.length === 0) return null;
	// API words are idx-ordered which should be time-ordered; sort defensively
	// so a mis-ordered aligner output cannot silently break the binary search.
	timedPositions.sort((a, b) => (words[a].start_s as number) - (words[b].start_s as number));
	const n = timedPositions.length;
	const timed = new Int32Array(n);
	const startsS = new Float64Array(n);
	const endsS = new Float64Array(n);
	const maxEndUpToS = new Float64Array(n);
	let reach = -Infinity;
	for (let k = 0; k < n; k++) {
		const w = words[timedPositions[k]];
		timed[k] = timedPositions[k];
		startsS[k] = w.start_s as number;
		endsS[k] = w.end_s as number;
		reach = Math.max(reach, w.end_s as number);
		maxEndUpToS[k] = reach;
	}
	return { words, timed, startsS, endsS, maxEndUpToS };
}

// -------------------------------------------------------------- lookup

/** First timed position whose start is strictly greater than t (upper bound). */
function _firstStartAfter(startsS: Float64Array, t: number): number {
	let lo = 0;
	let hi = startsS.length;
	while (lo < hi) {
		const mid = (lo + hi) >> 1;
		if (startsS[mid] <= t) lo = mid + 1;
		else hi = mid;
	}
	return lo;
}

/**
 * The active timed position at t, or -1 for a real gap.
 *
 * Both neighbours of the insertion point are inspected on purpose: with an
 * upper bound, `t === start_s` lands the candidate at `bound - 1`, and with
 * a lower bound it lands at `bound`. Checking one of the two is the bug that
 * made every word's first sample read as a gap in the sibling build. The
 * backward walk past `bound - 1` is guarded by `maxEndUpToS`, so it stops
 * immediately on the normal non-overlapping case and stays correct if an
 * aligner ever emits an overlap.
 */
export function activeWordAt(index: WordIndex, t: number): number {
	const bound = _firstStartAfter(index.startsS, t);
	// Neighbour ON the insertion point: only reachable when starts tie with t.
	if (bound < index.startsS.length && index.startsS[bound] <= t && t < index.endsS[bound]) {
		return bound;
	}
	for (let k = bound - 1; k >= 0; k--) {
		if (index.startsS[k] <= t && t < index.endsS[k]) return k;
		if (index.maxEndUpToS[k] <= t) return -1;
	}
	return -1;
}

// -------------------------------------------------------------- the rule

export type PointerKind = 'inside' | 'before' | 'after' | 'gap';

export interface PointerWord {
	kind: PointerKind;
	/** The pointer's resolved track time in seconds. */
	timeS: number;
	/** `words[]` index of the focus word (bold in the readout); null in a
	 * gap too far from any word to point at one. */
	wordIdx: number | null;
	/** Onset/offset seconds of the focus word; null exactly when wordIdx is. */
	focusStartS: number | null;
	focusEndS: number | null;
	/** `words[]` indices of the timed neighbours; null at the track edges.
	 * While inside a word these are the words either side of the active one. */
	prevWordIdx: number | null;
	nextWordIdx: number | null;
	/** 0..1 through the active word; null unless kind is 'inside'. */
	progress: number | null;
	/** Gap geometry; null while a word is active (INVARIANT 1). */
	gapToNextS: number | null;
	sincePrevS: number | null;
	/** Signed seconds pointer -> focus onset (positive = ahead); null when
	 * there is no focus word. */
	deltaS: number | null;
}

export interface ResolveOptions {
	/** Adjacency band in seconds; see nearSecondsForScale. */
	nearS: number;
}

const _NONE: Omit<PointerWord, 'kind' | 'timeS'> = {
	wordIdx: null,
	focusStartS: null,
	focusEndS: null,
	prevWordIdx: null,
	nextWordIdx: null,
	progress: null,
	gapToNextS: null,
	sincePrevS: null,
	deltaS: null
};

/** The pointer's word state at track time `t`. Pure; no DOM, no clock. */
export function resolvePointerWord(
	index: WordIndex | null,
	t: number,
	options: ResolveOptions
): PointerWord {
	if (index === null) return { kind: 'gap', timeS: t, ..._NONE };
	if (!Number.isFinite(t)) throw new RangeError(`pointer time must be finite, got ${t}`);

	const active = activeWordAt(index, t);
	const bound = _firstStartAfter(index.startsS, t);
	const nextPos = bound < index.startsS.length ? bound : -1;
	// The previous word is the one before the insertion point; when a word is
	// active that IS the active word, so step back past it for the neighbour.
	let prevPos = -1;
	for (let k = bound - 1; k >= 0; k--) {
		if (k === active) continue;
		prevPos = k;
		break;
	}
	const prevWordIdx = prevPos >= 0 ? index.timed[prevPos] : null;
	const nextWordIdx = nextPos >= 0 ? index.timed[nextPos] : null;

	if (active >= 0) {
		const startS = index.startsS[active];
		const endS = index.endsS[active];
		const span = endS - startS;
		return {
			kind: 'inside',
			timeS: t,
			wordIdx: index.timed[active],
			focusStartS: startS,
			focusEndS: endS,
			prevWordIdx,
			nextWordIdx,
			progress: span > 0 ? Math.min(1, Math.max(0, (t - startS) / span)) : 1,
			// INVARIANT 1: a word is sounding, so there is no gap to count down.
			gapToNextS: null,
			sincePrevS: null,
			deltaS: startS - t
		};
	}

	const gapToNextS = nextPos >= 0 ? index.startsS[nextPos] - t : Infinity;
	const sincePrevS = prevPos >= 0 ? t - index.endsS[prevPos] : Infinity;
	const kind: PointerKind =
		gapToNextS <= options.nearS && gapToNextS <= sincePrevS
			? 'before'
			: sincePrevS <= options.nearS
				? 'after'
				: 'gap';
	const focusPos = kind === 'before' ? nextPos : kind === 'after' ? prevPos : -1;
	return {
		kind,
		timeS: t,
		wordIdx: focusPos >= 0 ? index.timed[focusPos] : null,
		focusStartS: focusPos >= 0 ? index.startsS[focusPos] : null,
		focusEndS: focusPos >= 0 ? index.endsS[focusPos] : null,
		prevWordIdx,
		nextWordIdx,
		progress: null,
		gapToNextS: Number.isFinite(gapToNextS) ? gapToNextS : null,
		sincePrevS: Number.isFinite(sincePrevS) ? sincePrevS : null,
		deltaS: focusPos >= 0 ? index.startsS[focusPos] - t : null
	};
}

// -------------------------------------------------------------- geometry

/** Pointer x on a strip of `widthPx` -> track seconds, clamped to the track. */
export function timeForPointer(xPx: number, widthPx: number, durationS: number): number {
	if (!Number.isFinite(widthPx) || widthPx <= 0) {
		throw new RangeError(`timeForPointer: bad strip width ${widthPx}`);
	}
	if (!Number.isFinite(durationS) || durationS <= 0) {
		throw new RangeError(`timeForPointer: bad duration ${durationS}`);
	}
	return Math.min(1, Math.max(0, xPx / widthPx)) * durationS;
}

/**
 * Timed positions of the words a `[t0, t1]` span touches (drag-to-loop).
 * Returns null when the span sits wholly in a gap - a loop is never
 * invented out of silence. Order of t0/t1 does not matter.
 */
export function wordSpanRange(
	index: WordIndex,
	t0: number,
	t1: number
): { firstPos: number; lastPos: number } | null {
	const lo = Math.min(t0, t1);
	const hi = Math.max(t0, t1);
	const activeLo = activeWordAt(index, lo);
	const firstPos = activeLo >= 0 ? activeLo : _firstStartAfter(index.startsS, lo);
	if (firstPos >= index.startsS.length) return null;
	const activeHi = activeWordAt(index, hi);
	const lastPos = activeHi >= 0 ? activeHi : _firstStartAfter(index.startsS, hi) - 1;
	if (lastPos < firstPos) return null;
	return { firstPos, lastPos };
}

// -------------------------------------------------------------- readout

export interface ReadoutWord {
	/** `words[]` index (stable key for the render loop). */
	idx: number;
	text: string;
	role: 'focus' | 'dim';
	/** True when the pointer sits in the real gap right after this word. */
	gapAfter: boolean;
}

/**
 * The words to show around the pointer: the focus word plus up to `radius`
 * display-order neighbours either side. In a far gap there is no focus, so
 * the surrounding words render dimmed with the gap marked between them.
 */
export function readoutWords(
	index: WordIndex,
	state: PointerWord,
	radius: number = 2
): ReadoutWord[] {
	if (!Number.isInteger(radius) || radius < 0) {
		throw new RangeError(`readoutWords: bad radius ${radius}`);
	}
	const centre = state.wordIdx ?? state.prevWordIdx ?? state.nextWordIdx;
	if (centre === null) return [];
	const first = Math.max(0, centre - radius);
	const last = Math.min(index.words.length - 1, centre + radius);
	const gapAfterIdx =
		state.kind !== 'inside' && state.prevWordIdx !== null && state.nextWordIdx !== null
			? state.prevWordIdx
			: null;
	const out: ReadoutWord[] = [];
	for (let i = first; i <= last; i++) {
		out.push({
			idx: i,
			text: index.words[i].word,
			role: i === state.wordIdx ? 'focus' : 'dim',
			gapAfter: i === gapAfterIdx
		});
	}
	return out;
}
