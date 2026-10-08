/**
 * Lyrics library-column formatting + sort - pure functions so the
 * node:test harness (tests/unit/lyric-column-format.test.mjs) covers the
 * glyph / percent / paragraph / sort logic without mounting Svelte.
 *
 * Consumers: TrackTable.svelte (cell + hover panel via LyricTip.svelte)
 * and pane-contract's sortValue ('lyrics' SortKey).
 */

import type { LyricLine } from '$lib/api';
import type { LyricsRowSummary } from '$lib/rb/lyrics/types';
import { LYRIC_SHEET_PATH, MUSIC_NOTE_PATH } from '../../../ui/icon-glyphs';

type LyricVerdict = LyricsRowSummary['effective'];

/** The one stem fact the unchecked title reads: the listing row's stem
 * summary status (api-rb StemSummary, structurally). Declared here rather
 * than imported so this column module does not add a fan-in edge to api-rb. */
export type LyricStemStatus = { status: 'none' | 'invalid' | 'ready' };

/** A verdict mark is either an inline SVG icon path or a compact text mark.
 * The vocal verdict is an icon, not a musical-note character (CHROME-01:
 * icons, not emoji or text-symbol glyphs, in UI chrome). */
export type LyricVerdictMark =
	| { kind: 'icon'; path: string }
	| { kind: 'pending'; path: string }
	| { kind: 'text'; text: string };

/** Lyrics exist but nothing has judged them (LYR-13): a lyric-sheet icon with
 * a small pending dot. Distinct from the bare "?", which stays for an
 * unknown verdict with no lyric text behind it. */
export const LYRIC_UNCHECKED_MARK: LyricVerdictMark = { kind: 'pending', path: LYRIC_SHEET_PATH };

/** Compact per-verdict mark for the 64px library column. */
export const LYRIC_VERDICT_MARKS: Record<LyricVerdict, LyricVerdictMark> = {
	vocal: { kind: 'icon', path: MUSIC_NOTE_PATH }, // vocal, lyrics expected
	sparse: { kind: 'text', text: '~' }, // sparse vocal content
	'no-lyrics': { kind: 'text', text: '∅' }, // ∅ - calibrated no-lyrics
	unknown: { kind: 'text', text: '?' }
};

/** Hover explainer per verdict (glyph title). */
export const LYRIC_VERDICT_TITLES: Record<LyricVerdict, string> = {
	vocal: 'verdict: vocal - lyrics expected (from stem vocal coverage)',
	sparse: 'verdict: sparse - little vocal content (from stem vocal coverage)',
	'no-lyrics': 'verdict: no-lyrics - below the calibrated vocal-coverage band',
	unknown: 'verdict: unknown - not enough signal to judge'
};

/** Title for the sync-quality percent readout (house rule: every numeric
 * readout explains itself). */
export const LYRIC_QUALITY_TITLE =
	'sync quality: share of words the independent ASR witness does NOT distrust';

/** Title for the muted dash rendered when row.lyrics is null. */
export const LYRIC_NO_DATA_TITLE = 'no lyric data yet - pipeline has not processed this track';

export function lyricVerdictMark(verdict: LyricVerdict): LyricVerdictMark {
	const mark = LYRIC_VERDICT_MARKS[verdict];
	if (mark === undefined) {
		throw new Error(`lyricVerdictMark: unknown verdict "${String(verdict)}"`);
	}
	return mark;
}

export function lyricVerdictTitle(summary: LyricsRowSummary): string {
	const base = LYRIC_VERDICT_TITLES[summary.effective];
	if (base === undefined) {
		throw new Error(`lyricVerdictTitle: unknown verdict "${String(summary.effective)}"`);
	}
	return summary.override !== null ? `${base} (human override)` : base;
}

// ------------------------------------------------- cell state (LYR-13)

/** What the library cell shows for a row that HAS a lyrics summary:
 * - 'judged':    a vocal / sparse / no-lyrics verdict, shown as its verdict mark;
 * - 'unchecked': lyric text exists but the verdict is still unknown, so it is
 *                a found-but-unchecked lyric sheet, not doubt;
 * - 'unknown':   unknown verdict and no lyric text either, the bare "?".
 * Every current writer stamps 'unknown' only when there is NO signal (no stem
 * vocal coverage, or no verdict row at all) and a human override cannot be
 * 'unknown', so "judged unknown with signal" does not exist in the model. */
export type LyricCellState = 'judged' | 'unchecked' | 'unknown';

/** Lyric text behind the row: fetched lines or aligned words. */
export function lyricsHaveText(summary: LyricsRowSummary): boolean {
	return (summary.n_lines ?? 0) > 0 || summary.has_words;
}

export function lyricCellState(summary: LyricsRowSummary): LyricCellState {
	const verdict = summary.effective;
	switch (verdict) {
		case 'vocal':
		case 'sparse':
		case 'no-lyrics':
			return 'judged';
		case 'unknown':
			return lyricsHaveText(summary) ? 'unchecked' : 'unknown';
		default: {
			const _exhaustive: never = verdict;
			throw new Error(`lyricCellState: unknown verdict "${String(_exhaustive)}"`);
		}
	}
}

/** The mark the library cell and its hover tip draw for this row. */
export function lyricCellMark(summary: LyricsRowSummary): LyricVerdictMark {
	const state = lyricCellState(summary);
	switch (state) {
		case 'judged':
		case 'unknown':
			return lyricVerdictMark(summary.effective);
		case 'unchecked':
			return LYRIC_UNCHECKED_MARK;
		default: {
			const _exhaustive: never = state;
			throw new Error(`lyricCellMark: unhandled cell state "${String(_exhaustive)}"`);
		}
	}
}

/** Why there is no vocal-coverage verdict, read from the row's stem summary.
 * null = the listing has not delivered stems for this row yet (LIBM-172
 * lazy row assets), so the title says that rather than guessing. */
function _vocalCoverageReason(stems: LyricStemStatus | null): string {
	if (stems === null) return 'vocal coverage not measured (stem status not loaded for this row)';
	const status = stems.status;
	switch (status) {
		case 'none':
			return 'no stems, so no vocal coverage';
		case 'invalid':
			return 'stem bundle invalid, so no vocal coverage';
		case 'ready':
			return 'stems present but vocal coverage not measured yet';
		default: {
			const _exhaustive: never = status;
			throw new Error(`lyric cell title: unknown stem status "${String(_exhaustive)}"`);
		}
	}
}

/** Line count, or the word count when only alignment reported one. */
function _lyricAmount(summary: LyricsRowSummary): string {
	if (summary.n_lines !== null && summary.n_lines > 0) {
		return `${summary.n_lines} line${summary.n_lines === 1 ? '' : 's'}`;
	}
	if (summary.n_words !== null && summary.n_words > 0) {
		return `${summary.n_words} word${summary.n_words === 1 ? '' : 's'}`;
	}
	return 'aligned words';
}

/** Hover title for the cell mark: the verdict title for judged and bare
 * unknown rows; for an unchecked row, the line count and WHY it is unchecked,
 * built only from fields the row carries. */
export function lyricCellTitle(summary: LyricsRowSummary, stems: LyricStemStatus | null): string {
	const state = lyricCellState(summary);
	switch (state) {
		case 'judged':
		case 'unknown':
			return lyricVerdictTitle(summary);
		case 'unchecked': {
			const alignment = summary.has_words ? 'words aligned' : 'no word alignment';
			return (
				`lyrics found (${_lyricAmount(summary)}), not yet checked: ` +
				`${_vocalCoverageReason(stems)}; ${alignment}`
			);
		}
		default: {
			const _exhaustive: never = state;
			throw new Error(`lyricCellTitle: unhandled cell state "${String(_exhaustive)}"`);
		}
	}
}

/** Sync-quality whole percent = round((1 - pct_witness_red) * 100).
 * null in -> null out (nothing judged, no number to show). */
export function lyricSyncQualityPct(pctWitnessRed: number | null): number | null {
	if (pctWitnessRed === null) return null;
	if (!Number.isFinite(pctWitnessRed) || pctWitnessRed < 0 || pctWitnessRed > 1) {
		throw new Error(`lyricSyncQualityPct: pct_witness_red outside [0,1]: ${String(pctWitnessRed)}`);
	}
	return Math.round((1 - pctWitnessRed) * 100);
}

// ------------------------------------------------------------------- sort

/** Verdict major-order for the 'lyrics' SortKey (ascending = worst first,
 * so descending shows vocal tracks on top). An unchecked row (unknown
 * verdict, lyric text present) ranks above a bare unknown and no-lyrics
 * (LYR-13): it has lyrics, only the check is missing. */
const _VERDICT_RANK: Record<LyricVerdict, number> = {
	'no-lyrics': 0,
	unknown: 1,
	sparse: 3,
	vocal: 4
};
const _UNCHECKED_RANK = 2;

/**
 * Composite sort scalar: verdict rank major, sync quality minor.
 * A judged 0% still outranks "no percent at all" (-1), and rows with no
 * lyric data return null (pane-contract sorts nulls last).
 */
export function lyricsSortValue(summary: LyricsRowSummary | null): number | null {
	if (summary === null) return null;
	const verdictRank = _VERDICT_RANK[summary.effective];
	if (verdictRank === undefined) {
		throw new Error(`lyricsSortValue: unknown verdict "${String(summary.effective)}"`);
	}
	const rank = lyricCellState(summary) === 'unchecked' ? _UNCHECKED_RANK : verdictRank;
	const quality = lyricSyncQualityPct(summary.pct_witness_red) ?? -1;
	return rank * 1000 + quality;
}

// ------------------------------------------------------- tooltip paragraphs

/** Group server-derived lines into paragraphs on para_final (>= 2.5s gap);
 * the tooltip renders a blank gap between groups. A trailing group without
 * a para_final line is still a paragraph. */
export function groupLinesIntoParagraphs(lines: LyricLine[]): LyricLine[][] {
	const paragraphs: LyricLine[][] = [];
	let current: LyricLine[] = [];
	for (const line of lines) {
		current.push(line);
		if (line.para_final) {
			paragraphs.push(current);
			current = [];
		}
	}
	if (current.length > 0) paragraphs.push(current);
	return paragraphs;
}
