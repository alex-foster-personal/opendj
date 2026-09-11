/**
 * Adapter: daemon lyric payload -> LyricsTrack for the deck cursor.
 *
 * REDONE from the af--karaoke-ui-deck spike. The spike built lines and
 * per-line trust from local score heuristics (median / shaky-fraction /
 * hysteresis); the daemon now serves canonical lines with a CALIBRATED
 * witness `band` per line (apps/lyrics/lines.py), so this file no longer
 * guesses - it maps:
 *
 *   band 'good'      -> LINE fidelity 'word'  (full per-word wipe)
 *   band 'uncertain' -> 'line' (dimmed styling + explanatory title)
 *   band 'bad'       -> 'line' (never animate confidently over words the
 *                       witness distrusts)
 *   band 'unjudged'  -> 'line' (neutral styling)
 *
 * Pure model build: no I/O, no DOM, no clock.
 *
 * Fail-fast: missing lines, zero timed words, a non-finite timing, a zero
 * or negative span, a non-monotonic onset, or lines that do not partition
 * the timed words all THROW. The deck strip renders the thrown reason
 * rather than guessing - a karaoke display that lies about WHEN is worse
 * than none.
 */

import type { LyricTrack as ApiLyricTrack } from '$lib/api';
import type { LineFidelity, LyricLineBand, LyricsLine, LyricsTrack, LyricsWord } from './types';

export interface TrackMeta {
	id: string;
	artist: string;
	title: string;
	duration_s: number;
}

/** The one rule the strip renders by: only a server-judged GOOD line may
 * claim per-word timing. Everything else lights whole. */
export function bandFidelity(band: LyricLineBand): LineFidelity {
	return band === 'good' ? 'word' : 'line';
}

//------------------------------------------------------------------ helpers

function _timedWords(api: ApiLyricTrack): LyricsWord[] {
	const timed: LyricsWord[] = [];
	for (const w of api.words) {
		if (w.start_s == null || w.end_s == null) continue;
		timed.push({
			word: w.word,
			start_s: w.start_s,
			end_s: w.end_s,
			score: w.score ?? null,
			line_final: w.line_final,
			src_idx: w.idx
		});
	}
	return timed;
}

function _validate(words: readonly LyricsWord[], totalApiWords: number): void {
	if (words.length === 0) {
		throw new Error(`no word timings: ${totalApiWords} words in payload, none aligned`);
	}
	let previousStart = -Infinity;
	for (let i = 0; i < words.length; i += 1) {
		const w = words[i];
		if (!Number.isFinite(w.start_s) || !Number.isFinite(w.end_s)) {
			throw new Error(`word ${w.src_idx} ("${w.word}") has a non-finite timing`);
		}
		if (w.end_s <= w.start_s) {
			throw new Error(`word ${w.src_idx} ("${w.word}") has a zero or negative span`);
		}
		if (w.start_s < previousStart) {
			throw new Error(`word ${w.src_idx} ("${w.word}") starts before its predecessor`);
		}
		previousStart = w.start_s;
	}
}

function _buildLines(api: ApiLyricTrack, words: readonly LyricsWord[]): LyricsLine[] {
	const apiLines = api.lines;
	if (apiLines == null) {
		throw new Error('lyrics payload has no lines - fetch with include=lines');
	}
	const lines: LyricsLine[] = [];
	let cursor = 0; // walk position in `words`, which is src-ordered
	for (const apiLine of apiLines) {
		// Timed words for this line are the contiguous run whose src_idx falls
		// inside [first_idx, last_idx]. Lines with NO timed words cannot be
		// placed on the clock and are dropped (their text has nowhere honest
		// to appear); the coverage check below still guards the partition.
		const first = cursor;
		while (cursor < words.length && words[cursor].src_idx <= apiLine.last_idx) {
			if (words[cursor].src_idx < apiLine.first_idx) {
				throw new Error(
					`lyric lines do not partition the timed words: word idx ` +
						`${words[cursor].src_idx} precedes line [${apiLine.first_idx}..${apiLine.last_idx}]`
				);
			}
			cursor += 1;
		}
		if (cursor === first) continue; // fully untimed line
		lines.push({
			index: lines.length,
			text: apiLine.text,
			first_word: first,
			last_word: cursor - 1,
			start_s: words[first].start_s,
			end_s: words[cursor - 1].end_s,
			fidelity: bandFidelity(apiLine.band as LyricLineBand),
			band: apiLine.band as LyricLineBand,
			quality: apiLine.quality ?? null,
			n_red: apiLine.n_red,
			n_judged: apiLine.n_judged
		});
	}
	if (cursor !== words.length) {
		throw new Error(
			`lyric lines do not partition the timed words: ${words.length - cursor} timed ` +
				`words past the last line (first orphan idx ${words[cursor].src_idx})`
		);
	}
	return lines;
}

function _bandCounts(lines: readonly LyricsLine[]): Record<LyricLineBand, number> {
	const counts: Record<LyricLineBand, number> = { good: 0, uncertain: 0, bad: 0, unjudged: 0 };
	for (const line of lines) counts[line.band] += 1;
	return counts;
}

//------------------------------------------------------------------ adapter

export function adaptLyricTrack(meta: TrackMeta, api: ApiLyricTrack): LyricsTrack {
	const words = _timedWords(api);
	_validate(words, api.words.length);
	const lines = _buildLines(api, words);
	if (lines.length === 0) throw new Error('no line carries a timed word');
	return {
		id: meta.id,
		artist: meta.artist,
		title: meta.title,
		language_iso: api.verdict.language_iso3 ?? 'und',
		duration_s: meta.duration_s,
		words,
		lines,
		verdict: api.verdict.effective as import('$lib/api').LyricVerdictValue,
		word_fidelity_lines: lines.filter((line) => line.fidelity === 'word').length,
		band_counts: _bandCounts(lines)
	};
}
