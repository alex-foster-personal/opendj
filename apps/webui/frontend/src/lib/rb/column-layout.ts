/**
 * Library TrackTable column default widths / resize bounds.
 *
 * General policy: default width = content-fit for compact numeric/icon cols
 * (plays, key, bpm, time, order). Wider text cols (title, artist, comments,
 * genre) use chosen defaults (px) with min/max for resize; titles can be
 * long so we pick a responsive default not max-content.
 */

export type LibraryColId =
	| 'funnel'
	| 'cloud'
	| 'order'
	| 'preview'
	| 'art'
	| 'title'
	| 'artist'
	| 'key'
	| 'bpm'
	| 'plays'
	| 'rating'
	| 'comments'
	| 'time'
	| 'genre';

/** Default column widths (px). `art` is density-snapped in TrackTable. */
export const COL_DEFAULTS: Record<LibraryColId, number> = {
	funnel: 24,
	cloud: 24,
	order: 34,
	preview: 177,
	art: 22,
	title: 220,
	artist: 140,
	key: 48,
	bpm: 54,
	/** Header ▶ floor; TrackTable overrides from max play_count. */
	plays: 24,
	rating: 80,
	comments: 110,
	time: 48,
	genre: 90
};

/** Resize floor (px). */
export const COL_MIN: Record<LibraryColId, number> = {
	funnel: 20,
	cloud: 20,
	order: 28,
	preview: 80,
	art: 16,
	title: 80,
	artist: 60,
	key: 28,
	bpm: 28,
	plays: 24,
	rating: 60,
	comments: 40,
	time: 36,
	genre: 40
};

/** Resize ceiling (px). */
export const COL_MAX: Record<LibraryColId, number> = {
	funnel: 48,
	cloud: 48,
	order: 72,
	preview: 420,
	art: 72,
	title: 640,
	artist: 420,
	key: 96,
	bpm: 96,
	plays: 96,
	rating: 140,
	comments: 480,
	time: 96,
	genre: 320
};

// ----- plays content-fit -----

/** Tabular digit advance at `.c-plays` font-size 10px (approx). */
const PLAYS_DIGIT_PX = 6;
/** Compact `--tt-td-pad-x` each side. */
const PLAYS_PAD_X = 6;
/** ▶ header glyph content width (must fit even when all counts are 0). */
const PLAYS_HEADER_CONTENT_PX = 12;

/**
 * Default plays column width (px) for the largest play_count in view,
 * or at least the ▶ header icon. Cheap: digit count only + padding.
 */
export function playsWidthForMaxCount(maxPlayCount: number): number {
	const n = Number.isFinite(maxPlayCount) ? Math.max(0, Math.floor(maxPlayCount)) : 0;
	const digits = n === 0 ? 1 : String(n).length;
	const content = Math.max(digits * PLAYS_DIGIT_PX, PLAYS_HEADER_CONTENT_PX);
	return Math.ceil(content + 2 * PLAYS_PAD_X);
}

/** Max play_count across provider rows (0 when empty). */
export function maxPlayCountOf(
	rows: ReadonlyArray<{ play_count?: number | null }>
): number {
	let max = 0;
	for (const row of rows) {
		const n = row.play_count;
		if (typeof n === 'number' && Number.isFinite(n) && n > max) max = n;
	}
	return max;
}
