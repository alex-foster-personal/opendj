/**
 * Requirements: specs/features/karaoke-deck-panel/REQUIREMENTS.md (R2, R3, R6)
 *
 * Clock -> cursor resolution. Pure: takes a presented SOURCE-TRACK position in
 * seconds and returns which word / line is live, plus the honest gap states.
 *
 * Cost: O(1) amortised. A monotonic hint walks forward during normal playback
 * (0-1 steps per frame); any backwards jump or stale hint falls back to a
 * binary search, so a seek or a rate change re-syncs on the very next frame
 * without special-casing either.
 */

import type { LyricsFrame, LyricsLine, LyricsPlaybackState, LyricsTrack } from './types';
import type { LyricsFidelity } from './types';

/** A silence longer than this between lines is an instrumental, not a pause. */
export const INSTRUMENTAL_GAP_S = 6.0;
/** Words closer together than this belong to one contiguous vocal region. */
export const REGION_MERGE_GAP_S = 1.5;

export interface LyricsCursor {
	readonly state: LyricsPlaybackState;
	readonly lineIndex: number | null;
	/** Half-open [start_s, end_s) hit, or null in an inter-word gap. */
	readonly wordIndex: number | null;
	readonly nextLineIndex: number | null;
	readonly lineProgress: number;
	readonly wordProgress: number | null;
	readonly nextVocalInMs: number | null;
	/** Feed back as `hint` next frame. */
	readonly hint: number;
}

export interface VocalRegion {
	readonly start_s: number;
	readonly end_s: number;
}

/** Word index -> line index, built once per track. */
export function buildWordLineMap(track: LyricsTrack): Int32Array {
	const map = new Int32Array(track.words.length);
	for (const line of track.lines) {
		for (let i = line.first_word; i <= line.last_word; i += 1) map[i] = line.index;
	}
	return map;
}

/** Contiguous sung stretches, for the `region` rung of the fidelity ladder. */
export function buildVocalRegions(track: LyricsTrack): VocalRegion[] {
	const regions: VocalRegion[] = [];
	let start = track.words[0].start_s;
	let end = track.words[0].end_s;
	for (const word of track.words.slice(1)) {
		if (word.start_s - end > REGION_MERGE_GAP_S) {
			regions.push({ start_s: start, end_s: end });
			start = word.start_s;
		}
		end = word.end_s;
	}
	regions.push({ start_s: start, end_s: end });
	return regions;
}

/** Index of the last word whose start_s <= t, or -1 when t precedes the first. */
export function findWordIndex(track: LyricsTrack, t: number, hint: number): number {
	const words = track.words;
	if (t < words[0].start_s) return -1;
	if (hint >= 0 && hint < words.length && words[hint].start_s <= t) {
		// Forward walk: the common case is 0 or 1 steps.
		let i = hint;
		let steps = 0;
		while (i + 1 < words.length && words[i + 1].start_s <= t && steps < 4) {
			i += 1;
			steps += 1;
		}
		if (i + 1 >= words.length || words[i + 1].start_s > t) return i;
	}
	// Stale hint (seek, big rate jump, first frame): binary search.
	let lo = 0;
	let hi = words.length - 1;
	while (lo < hi) {
		const mid = (lo + hi + 1) >> 1;
		if (words[mid].start_s <= t) lo = mid;
		else hi = mid - 1;
	}
	return lo;
}

function _clamp01(value: number): number {
	if (value < 0) return 0;
	if (value > 1) return 1;
	return value;
}

function _lineProgress(line: LyricsLine, t: number): number {
	const span = line.end_s - line.start_s;
	if (span <= 0) return 1;
	return _clamp01((t - line.start_s) / span);
}

export function resolveCursor(
	track: LyricsTrack,
	wordLine: Int32Array,
	t: number,
	hint: number
): LyricsCursor {
	const words = track.words;
	const index = findWordIndex(track, t, hint);

	if (index < 0) {
		return {
			state: 'preroll',
			lineIndex: null,
			wordIndex: null,
			nextLineIndex: track.lines.length > 0 ? 0 : null,
			lineProgress: 0,
			wordProgress: null,
			nextVocalInMs: (words[0].start_s - t) * 1000,
			hint: 0
		};
	}

	const word = words[index];
	const lineIndex = wordLine[index];
	const line = track.lines[lineIndex];
	const nextLineIndex = lineIndex + 1 < track.lines.length ? lineIndex + 1 : null;

	// Half-open [start_s, end_s): the word is live only while it is sounding.
	if (t < word.end_s) {
		return {
			state: 'line',
			lineIndex,
			wordIndex: index,
			nextLineIndex,
			lineProgress: _lineProgress(line, t),
			wordProgress: _clamp01((t - word.start_s) / (word.end_s - word.start_s)),
			nextVocalInMs: null,
			hint: index
		};
	}

	// Past the word's end: a REAL gap with no active word.
	const next = index + 1 < words.length ? words[index + 1] : null;
	if (next === null) {
		return {
			state: 'outro',
			lineIndex,
			wordIndex: null,
			nextLineIndex: null,
			lineProgress: 1,
			wordProgress: null,
			nextVocalInMs: null,
			hint: index
		};
	}

	const gapMs = (next.start_s - t) * 1000;
	// A long silence is an instrumental whether or not it happens to fall on a
	// line boundary. Line breaks come from the lyric text, not from the audio,
	// so a 20s break often sits MID-line: holding that line lit for 20s while
	// nothing is sung is the same lie either way.
	const instrumental = next.start_s - word.end_s > INSTRUMENTAL_GAP_S;
	return {
		state: instrumental ? 'gap' : 'line',
		lineIndex,
		wordIndex: null,
		// Through an instrumental the useful preview is the line that resumes,
		// which is the line the NEXT word belongs to - sometimes this same one.
		nextLineIndex: instrumental ? wordLine[index + 1] : nextLineIndex,
		lineProgress: _lineProgress(line, t),
		wordProgress: null,
		nextVocalInMs: gapMs,
		hint: index
	};
}

/** Semantic payload for the deck panel, a crowd display and an LED bar alike. */
export function toLyricsFrame(
	track: LyricsTrack,
	cursor: LyricsCursor,
	t: number,
	fidelity: LyricsFidelity
): LyricsFrame {
	// Fail-fast on the one lie this display must never tell: a countdown to the
	// next vocal while a word is still sounding. Long sustained words (one real
	// aligner output holds a syllable for 29.4s) make a naive
	// nextWord.start_s - now announce ~25s of "silence" over the top of the
	// singing. Crashing beats rendering that.
	if (cursor.wordIndex !== null && cursor.nextVocalInMs !== null) {
		throw new Error(
			`lyrics frame invariant broken: countdown ${cursor.nextVocalInMs}ms while ` +
				`word ${cursor.wordIndex} ("${track.words[cursor.wordIndex].word}") is still active`
		);
	}
	if (cursor.wordIndex !== null && cursor.state !== 'line') {
		throw new Error(
			`lyrics frame invariant broken: state "${cursor.state}" while word ` +
				`${cursor.wordIndex} is still active`
		);
	}

	const current = cursor.lineIndex === null ? null : track.lines[cursor.lineIndex];
	const previous =
		cursor.lineIndex !== null && cursor.lineIndex > 0 ? track.lines[cursor.lineIndex - 1] : null;
	const next = cursor.nextLineIndex === null ? null : track.lines[cursor.nextLineIndex];
	const word = cursor.wordIndex === null ? null : track.words[cursor.wordIndex];
	return {
		schema: 'lyrics.frame/1',
		source_ms: Math.round(t * 1000),
		fidelity,
		state: cursor.state,
		line_index: cursor.lineIndex,
		word_index: cursor.wordIndex,
		lines: {
			prev: previous?.text ?? null,
			current: current?.text ?? null,
			next: next?.text ?? null
		},
		word_span_ms:
			word === null
				? null
				: { start: Math.round(word.start_s * 1000), end: Math.round(word.end_s * 1000) },
		progress: { line: cursor.lineProgress, word: cursor.wordProgress },
		next_vocal_in_ms: cursor.nextVocalInMs === null ? null : Math.round(cursor.nextVocalInMs),
		track: {
			id: track.id,
			language_iso: track.language_iso,
			word_count: track.words.length,
			line_count: track.lines.length,
			word_fidelity_lines: track.word_fidelity_lines
		}
	};
}

/** The line at `index`, or null: NEVER undefined. A cursor index survives a
 *  track switch for a frame, so it can point past the new track's lines
 *  (silver preview, Tue 6 Oct 2026 09:19:25Z: "Cannot read properties of
 *  undefined (reading 'fidelity')" in DeckLyricLine right after a load). */
export function lineAtOrNull(track: LyricsTrack | null, index: number | null): LyricsLine | null {
	if (track === null || index === null) return null;
	if (!Number.isInteger(index) || index < 0 || index >= track.lines.length) return null;
	return track.lines[index];
}
