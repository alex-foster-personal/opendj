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

type LyricVerdict = LyricsRowSummary['effective'];

/** Compact per-verdict glyph for the 64px library column. */
export const LYRIC_VERDICT_GLYPHS: Record<LyricVerdict, string> = {
	vocal: '♪', // ♪ - vocal, lyrics expected
	sparse: '~', // sparse vocal content
	'no-lyrics': '∅', // ∅ - calibrated no-lyrics
	unknown: '?'
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

export function lyricVerdictGlyph(verdict: LyricVerdict): string {
	const glyph = LYRIC_VERDICT_GLYPHS[verdict];
	if (glyph === undefined) {
		throw new Error(`lyricVerdictGlyph: unknown verdict "${String(verdict)}"`);
	}
	return glyph;
}

export function lyricVerdictTitle(summary: LyricsRowSummary): string {
	const base = LYRIC_VERDICT_TITLES[summary.effective];
	if (base === undefined) {
		throw new Error(`lyricVerdictTitle: unknown verdict "${String(summary.effective)}"`);
	}
	return summary.override !== null ? `${base} (human override)` : base;
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
 * so descending shows vocal tracks on top). */
const _VERDICT_RANK: Record<LyricVerdict, number> = {
	'no-lyrics': 0,
	unknown: 1,
	sparse: 2,
	vocal: 3
};

/**
 * Composite sort scalar: verdict rank major, sync quality minor.
 * A judged 0% still outranks "no percent at all" (-1), and rows with no
 * lyric data return null (pane-contract sorts nulls last).
 */
export function lyricsSortValue(summary: LyricsRowSummary | null): number | null {
	if (summary === null) return null;
	const rank = _VERDICT_RANK[summary.effective];
	if (rank === undefined) {
		throw new Error(`lyricsSortValue: unknown verdict "${String(summary.effective)}"`);
	}
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
