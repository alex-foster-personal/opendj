/**
 * Stage frame maths - which line and word the STAGE overlay shows at a given
 * presented source position, adapted from the karaoke spike's frame.ts to the
 * daemon's LyricTrack/LyricLine/LyricWord shapes ($lib/api).
 *
 * THE CLOCK CONTRACT (do not reinvent)
 *   - Word timings are immutable SOURCE-TRACK milliseconds (converted once
 *     from the API's seconds in buildStageScore).
 *   - `ms` is the source position of the sample the output has ACTUALLY
 *     PRESENTED (HTMLMediaElement.currentTime or the deck's position_ms),
 *     never wall time and never wall time minus a latency guess.
 *   - Words are selected on the half-open interval [start_ms, end_ms).
 *   - The inter-word gap is a REAL state: no word is active. It is not "the
 *     previous word, still". A countdown is only meaningful when NO word is
 *     active (the 29.4s-sustained-word trap).
 *
 * Requirements (mini-PRD)
 *   R1 [if a 29.4s word is sounding then state is 'singing', never a countdown ⛔️]
 *   R2 [if ms equals a word's end_ms then that word is NOT active ⛔️]
 *   R3 [if ms equals a word's start_ms then that word IS active ⛔️]
 *   R4 [if a word has no aligned timing then it renders as 'untimed', and it
 *       never enters clock selection ⛔️]
 *   R5 [if lyrics were fetched without lines, or no word carries timing, then
 *       buildStageScore throws instead of guessing ⛔️]
 */

import type { LyricLine, LyricTrack, LyricWitness } from '$lib/api';

/** How long a finished line stays lit before the frame moves on. */
export const LINE_HOLD_MS = 1_500;
/** How far ahead of its first word a line pre-lights. */
export const LINE_PRELIGHT_MS = 4_000;

export type StageWordState = 'sung' | 'active' | 'unsung' | 'untimed';

/**
 * Coarse narrative state, derived not authored.
 *   lead-in      before the first aligned word of the track
 *   singing      a word is active
 *   gap          inside or adjacent to a line, but between words
 *   instrumental no line is in play and the next one is far away
 *   outro        every aligned word is finished
 */
export type StagePlayState = 'lead-in' | 'singing' | 'gap' | 'instrumental' | 'outro';

export interface StageWord {
	/** Original word idx from the API payload. */
	idx: number;
	text: string;
	start_ms: number | null;
	end_ms: number | null;
	witness: LyricWitness | null;
}

export interface StageLine {
	/** Position in StageScore.lines (display order). */
	index: number;
	text: string;
	start_ms: number | null;
	end_ms: number | null;
	band: LyricLine['band'];
	n_words: number;
	n_red: number;
	n_judged: number;
	para_final: boolean;
	words: StageWord[];
}

/** One aligned word flattened for binary search. */
export interface TimedWord {
	/** Onset, source-track ms. */
	a: number;
	/** Offset, source-track ms, exclusive. */
	b: number;
	/** Index into StageScore.lines. */
	line: number;
	/** Position within that line's words array. */
	word: number;
}

interface TimedLine {
	a: number;
	b: number;
	/** Index into StageScore.lines. */
	line: number;
}

export interface StageScore {
	lines: StageLine[];
	/** Only words with BOTH timings, sorted by onset. */
	timedWords: TimedWord[];
	/** Only lines with BOTH timings, sorted by onset. */
	timedLines: TimedLine[];
}

//-----------------------------------------------------------------------------
// score build
//-----------------------------------------------------------------------------

function _sMs(seconds: number | null): number | null {
	return seconds === null ? null : Math.round(seconds * 1000);
}

/** Compile a LyricTrack into the stage's lookup structure. Fails fast on
 *  payloads the stage cannot honestly render (R5). */
export function buildStageScore(track: LyricTrack): StageScore {
	const apiLines = track.lines;
	if (apiLines == null) {
		throw new Error('stage requires lyrics fetched with include=lines');
	}
	if (track.words.length === 0) {
		throw new Error('stage requires at least one word');
	}
	const lines: StageLine[] = apiLines.map((line, index) => ({
		index,
		text: line.text,
		start_ms: _sMs(line.start_s),
		end_ms: _sMs(line.end_s),
		band: line.band,
		n_words: line.n_words,
		n_red: line.n_red,
		n_judged: line.n_judged,
		para_final: line.para_final,
		words: []
	}));

	// Two-pointer walk: words and lines are both in idx order.
	const timedWords: TimedWord[] = [];
	let li = 0;
	for (const w of track.words) {
		while (li < apiLines.length && w.idx > apiLines[li].last_idx) li += 1;
		if (li >= apiLines.length || w.idx < apiLines[li].first_idx) {
			throw new Error(`word idx ${w.idx} is not covered by any derived line`);
		}
		const start_ms = _sMs(w.start_s ?? null);
		const end_ms = _sMs(w.end_s ?? null);
		const stageWord: StageWord = {
			idx: w.idx,
			text: w.word,
			start_ms,
			end_ms,
			witness: w.witness ?? null
		};
		const line = lines[li];
		if (start_ms !== null && end_ms !== null) {
			timedWords.push({ a: start_ms, b: end_ms, line: li, word: line.words.length });
		}
		line.words.push(stageWord);
	}
	if (timedWords.length === 0) {
		throw new Error('no words carry aligned timings - nothing for the stage clock to follow');
	}
	timedWords.sort((x, y) => x.a - y.a);

	const timedLines: TimedLine[] = [];
	for (const line of lines) {
		if (line.start_ms !== null && line.end_ms !== null) {
			timedLines.push({ a: line.start_ms, b: line.end_ms, line: line.index });
		}
	}
	timedLines.sort((x, y) => x.a - y.a);
	return { lines, timedWords, timedLines };
}

//-----------------------------------------------------------------------------
// lookup (all half-open [a, b))
//-----------------------------------------------------------------------------

/** Index into timedWords of the word whose [a, b) contains `ms`, or null. */
export function findActiveTimedIndex(score: StageScore, ms: number): number | null {
	const words = score.timedWords;
	let low = 0;
	let high = words.length - 1;
	while (low <= high) {
		const mid = (low + high) >> 1;
		const word = words[mid];
		if (ms < word.a) high = mid - 1;
		else if (ms >= word.b) low = mid + 1;
		else return mid;
	}
	return null;
}

function _findNextTimedWordIndex(score: StageScore, ms: number): number | null {
	const words = score.timedWords;
	let low = 0;
	let high = words.length - 1;
	let found: number | null = null;
	while (low <= high) {
		const mid = (low + high) >> 1;
		if (words[mid].a > ms) {
			found = mid;
			high = mid - 1;
		} else low = mid + 1;
	}
	return found;
}

/** score.lines index of the line whose [a, b) contains `ms`, or null. */
function _findContainingLineIndex(score: StageScore, ms: number): number | null {
	const lines = score.timedLines;
	let low = 0;
	let high = lines.length - 1;
	while (low <= high) {
		const mid = (low + high) >> 1;
		const line = lines[mid];
		if (ms < line.a) high = mid - 1;
		else if (ms >= line.b) low = mid + 1;
		else return line.line;
	}
	return null;
}

/** Timed line that has fully finished by `ms` (latest), or null. */
function _findPreviousTimedLine(score: StageScore, ms: number): TimedLine | null {
	const lines = score.timedLines;
	let low = 0;
	let high = lines.length - 1;
	let found: TimedLine | null = null;
	while (low <= high) {
		const mid = (low + high) >> 1;
		if (lines[mid].b <= ms) {
			found = lines[mid];
			low = mid + 1;
		} else high = mid - 1;
	}
	return found;
}

/** First timed line that has not started by `ms`, or null. */
function _findNextTimedLine(score: StageScore, ms: number): TimedLine | null {
	const lines = score.timedLines;
	let low = 0;
	let high = lines.length - 1;
	let found: TimedLine | null = null;
	while (low <= high) {
		const mid = (low + high) >> 1;
		if (lines[mid].a > ms) {
			found = lines[mid];
			high = mid - 1;
		} else low = mid + 1;
	}
	return found;
}

//-----------------------------------------------------------------------------
// anchor resolution (the only per-animation-frame work)
//-----------------------------------------------------------------------------

export interface StageAnchor {
	/** Line the stage should treat as "current" (score.lines index), or null. */
	lineIndex: number | null;
	/** Active word as an index into score.timedWords, or null. */
	timedWordIndex: number | null;
	state: StagePlayState;
	/** Ms until the next aligned word onset while waiting, else null. */
	gapMs: number | null;
	/** Line to preview during an instrumental, even though nothing is current. */
	upcomingLineIndex: number | null;
}

/** Resolve which line and word the stage shows at `ms`. Cheap enough for
 *  every animation frame; callers compare anchorKey and rebuild the (much
 *  larger) frame only when it changes. */
export function resolveAnchor(score: StageScore, ms: number): StageAnchor {
	const timedWordIndex = findActiveTimedIndex(score, ms);
	if (timedWordIndex !== null) {
		return {
			lineIndex: score.timedWords[timedWordIndex].line,
			timedWordIndex,
			state: 'singing',
			gapMs: null,
			upcomingLineIndex: null
		};
	}

	const nextLine = _findNextTimedLine(score, ms);
	const nextWordIndex = _findNextTimedWordIndex(score, ms);
	const gapMs = nextWordIndex === null ? null : score.timedWords[nextWordIndex].a - ms;
	const nextLineIndex = nextLine === null ? null : nextLine.line;

	const containing = _findContainingLineIndex(score, ms);
	if (containing !== null) {
		// Between two words of a line that is still in play.
		return {
			lineIndex: containing,
			timedWordIndex: null,
			state: 'gap',
			gapMs,
			upcomingLineIndex: nextLineIndex
		};
	}

	const previous = _findPreviousTimedLine(score, ms);
	if (previous !== null && ms - previous.b < LINE_HOLD_MS) {
		// Just finished. Hold it lit rather than snapping to darkness.
		return {
			lineIndex: previous.line,
			timedWordIndex: null,
			state: 'gap',
			gapMs,
			upcomingLineIndex: nextLineIndex
		};
	}
	if (nextLine !== null && nextLine.a - ms <= LINE_PRELIGHT_MS) {
		// About to start. Pre-light it so the room can draw breath.
		return {
			lineIndex: nextLine.line,
			timedWordIndex: null,
			state: 'gap',
			gapMs,
			upcomingLineIndex: nextLineIndex
		};
	}
	if (nextLine === null) {
		return {
			lineIndex: null,
			timedWordIndex: null,
			state: 'outro',
			gapMs: null,
			upcomingLineIndex: null
		};
	}
	return {
		lineIndex: null,
		timedWordIndex: null,
		state: previous === null ? 'lead-in' : 'instrumental',
		gapMs,
		upcomingLineIndex: nextLineIndex
	};
}

/** Stable identity of an anchor, so callers can skip work when it repeats. */
export function anchorKey(anchor: StageAnchor): string {
	return `${anchor.lineIndex}:${anchor.timedWordIndex}:${anchor.state}:${anchor.upcomingLineIndex}`;
}

//-----------------------------------------------------------------------------
// frame build (on anchor change only - too heavy for 60Hz)
//-----------------------------------------------------------------------------

export interface StageFrameWord extends StageWord {
	state: StageWordState;
}

export interface StageFrameLine {
	index: number;
	text: string;
	start_ms: number | null;
	end_ms: number | null;
	band: LyricLine['band'];
	n_words: number;
	n_red: number;
	n_judged: number;
	para_final: boolean;
	words: StageFrameWord[];
}

export interface StageFrame {
	/** Presented source-track position at build time, ms. */
	source_ms: number;
	state: StagePlayState;
	/** Ms until the next aligned word onset, else null. NEVER non-null while a
	 *  word is active (the countdown invariant). */
	gap_ms: number | null;
	line_index: number | null;
	lines: {
		prev: StageFrameLine | null;
		current: StageFrameLine | null;
		next: StageFrameLine | null;
	};
	/** Span of the active word, or null in a gap. */
	word_span_ms: { start: number; end: number } | null;
	/** 0..1 through lines.current (0 when the line carries no timing). */
	progress: number;
	/** 0..1 through the active word. Drives the fill sweep. */
	word_progress: number;
}

export function clamp01(value: number): number {
	if (!Number.isFinite(value)) return 0;
	if (value < 0) return 0;
	if (value > 1) return 1;
	return value;
}

function _buildFrameLine(
	score: StageScore,
	lineIndex: number | null,
	ms: number
): StageFrameLine | null {
	if (lineIndex === null || lineIndex < 0 || lineIndex >= score.lines.length) return null;
	const line = score.lines[lineIndex];
	const words: StageFrameWord[] = line.words.map((word) => {
		let state: StageWordState = 'untimed';
		if (word.start_ms !== null && word.end_ms !== null) {
			if (ms >= word.end_ms) state = 'sung';
			else if (ms >= word.start_ms) state = 'active';
			else state = 'unsung';
		}
		return { ...word, state };
	});
	return {
		index: line.index,
		text: line.text,
		start_ms: line.start_ms,
		end_ms: line.end_ms,
		band: line.band,
		n_words: line.n_words,
		n_red: line.n_red,
		n_judged: line.n_judged,
		para_final: line.para_final,
		words
	};
}

/** 0..1 through the active word - the ONLY value the animation frame writes
 *  (as a CSS custom property), so a sustained word sweeps without a single
 *  structural DOM rewrite. */
export function wordProgress01(
	score: StageScore,
	timedWordIndex: number | null,
	ms: number
): number {
	if (timedWordIndex === null) return 0;
	const word = score.timedWords[timedWordIndex];
	return clamp01((ms - word.a) / (word.b - word.a));
}

export function buildFrame(
	score: StageScore,
	ms: number,
	anchor: StageAnchor = resolveAnchor(score, ms)
): StageFrame {
	const current = _buildFrameLine(score, anchor.lineIndex, ms);
	const prev = _buildFrameLine(score, anchor.lineIndex === null ? null : anchor.lineIndex - 1, ms);
	const nextIndex = anchor.lineIndex === null ? anchor.upcomingLineIndex : anchor.lineIndex + 1;
	const next = _buildFrameLine(score, nextIndex, ms);

	const activeWord = anchor.timedWordIndex === null ? null : score.timedWords[anchor.timedWordIndex];
	const lineSpan =
		current === null || current.start_ms === null || current.end_ms === null
			? 0
			: current.end_ms - current.start_ms;

	return {
		source_ms: Math.round(ms),
		state: anchor.state,
		gap_ms: anchor.gapMs === null ? null : Math.round(anchor.gapMs),
		line_index: anchor.lineIndex,
		lines: { prev, current, next },
		word_span_ms: activeWord === null ? null : { start: activeWord.a, end: activeWord.b },
		progress:
			current === null || current.start_ms === null || lineSpan <= 0
				? 0
				: clamp01((ms - current.start_ms) / lineSpan),
		word_progress: wordProgress01(score, anchor.timedWordIndex, ms)
	};
}

//-----------------------------------------------------------------------------
// presentation helpers
//-----------------------------------------------------------------------------

/** The interlude/countdown card predicate - ONE implementation, consumed by
 *  the overlay and the regression tests, so "countdown while a word is
 *  active" cannot re-emerge from a second local copy of the rule. */
export function showInterludeCard(state: StagePlayState): boolean {
	return state === 'lead-in' || state === 'instrumental' || state === 'outro';
}

/** Shrink factor for a long line so it still fits the stage. Character count
 *  is a crude width proxy but is stable and never fights layout mid-playback. */
export function lineFitScale(text: string): number {
	const characters = text.length;
	if (characters <= 26) return 1;
	return Math.max(0.5, 26 / characters);
}

export function formatClock(seconds: number): string {
	if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
	const whole = Math.floor(seconds);
	const minutes = Math.floor(whole / 60);
	return `${minutes}:${String(whole % 60).padStart(2, '0')}`;
}
