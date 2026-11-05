/**
 * The library "Vocals" checkbox (VOCALS-FILTER-01, the maintainer Tue 6 Oct 2026:
 * "filtering by vocals shows very few songs on all tracks - check for bug").
 *
 * A DJ ticking "Vocals" means "this track has vocals", not "the karaoke
 * pipeline aligned more than five lines of words". The old rule read only
 * `lyrics.n_lines`, which the listing filled for a few dozen aligned tracks, so
 * the filter kept ~18 of 2,627 present tracks.
 *
 * Each row now gets a three-way verdict from everything the listing knows:
 *
 * - `yes`: vocal regions (rekordbox PVDI or demucs detection with at least one
 *   region), OR the lyric pipeline's stem-coverage verdict is `vocal`, OR the
 *   track has at least VOCALS_FILTER_MIN_LINES lines of lyrics.
 * - `no`: vocal analysis ran and found none (`no_vocals`, or a rekordbox/demucs
 *   answer with zero regions), or the lyric verdict is `no-lyrics`.
 * - `unknown`: nothing has measured it. Index rows (LIBM-172) carry
 *   `not_analyzed` vocals until their row assets load in view, so unknown rows
 *   are excluded and COUNTED in the tooltip rather than guessed.
 *
 * Positive evidence wins over negative: lyrics on a track rekordbox marked
 * vocal-free are still sung words. The one exception is a `no-lyrics` lyric
 * verdict, which overrides that track's own line count (but not vocal regions).
 *
 * Pure and runeless so tests/unit/vocals-filter.test.mjs covers it directly.
 */

// Structural slices of BrowserRow's `vocals` (api-rb Vocals) and `lyrics`
// (LyricsRowSummary), declared here rather than imported so this leaf adds no
// importer to api-rb, the repo's most-imported module (quality ratchet fan-in).
type VocalsSlice =
	| { status: 'rekordbox' | 'no_vocals' | 'demucs'; regions: readonly unknown[] }
	| { status: 'not_analyzed' };
type LyricsSlice = {
	effective: 'vocal' | 'sparse' | 'no-lyrics' | 'unknown';
	n_lines: number | null;
};

/** Lyrics count as vocals at MORE than 5 lines (a one-line hook is a chop). */
export const VOCALS_FILTER_MIN_LINES = 6;

export type VocalsVerdict = 'yes' | 'no' | 'unknown';

export interface VocalsFilterRow {
	vocals: VocalsSlice;
	lyrics: LyricsSlice | null;
}

export function rowVocalsVerdict(row: VocalsFilterRow): VocalsVerdict {
	const v = row.vocals;
	const regionsFound = (v.status === 'rekordbox' || v.status === 'demucs') && v.regions.length > 0;
	const lyrics = row.lyrics;
	// A no-lyrics verdict (often a human override) outranks a line count: the
	// cached lines are the ones it judged not to be lyrics. Vocal regions still win.
	const lyricLines =
		lyrics !== null &&
		lyrics.effective !== 'no-lyrics' &&
		lyrics.n_lines !== null &&
		lyrics.n_lines >= VOCALS_FILTER_MIN_LINES;
	if (regionsFound || lyricLines || lyrics?.effective === 'vocal') return 'yes';
	const analyzedNone =
		v.status === 'no_vocals' || v.status === 'rekordbox' || v.status === 'demucs';
	if (analyzedNone || lyrics?.effective === 'no-lyrics') return 'no';
	return 'unknown';
}

/** Vocals checkbox predicate: only rows with positive vocal evidence pass. */
export function rowHasVocals(row: VocalsFilterRow): boolean {
	return rowVocalsVerdict(row) === 'yes';
}

export interface VocalsFilterCounts {
	matching: number;
	without: number;
	unknown: number;
}

export function vocalsFilterCounts(rows: readonly VocalsFilterRow[]): VocalsFilterCounts {
	const counts: VocalsFilterCounts = { matching: 0, without: 0, unknown: 0 };
	for (const row of rows) {
		const verdict = rowVocalsVerdict(row);
		if (verdict === 'yes') counts.matching += 1;
		else if (verdict === 'no') counts.without += 1;
		else counts.unknown += 1;
	}
	return counts;
}

/** Checkbox tooltip: the rule, then the counts for the rows in this pane. */
export function vocalsFilterTitle(counts: VocalsFilterCounts): string {
	return (
		'Keep only tracks with vocals: vocal regions from rekordbox or stem detection, ' +
		`a "vocal" lyric verdict, or at least ${VOCALS_FILTER_MIN_LINES} lines of lyrics. ` +
		`In this list: ${counts.matching} with vocals, ${counts.without} without, ` +
		`${counts.unknown} not analyzed yet (excluded until vocals or lyrics are known).`
	);
}
