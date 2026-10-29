/**
 * Which library columns the user has hidden (library-organization UI
 * contract, row "Columns": right-click the track table header for a chooser;
 * visibility persists in ui-prefs `library_hidden_columns`).
 *
 * The order here is the table's left-to-right order, and it is mirrored by
 * `_LIBRARY_HIDEABLE_COLUMNS` in apps/webui/server/routes/ui_prefs.py
 * (tests/webui/test_ui_prefs_library_hidden_columns.py holds the two equal).
 *
 * Not hideable, on purpose:
 * - funnel, err, cloud, order: utility columns that carry row state and the
 *   reorder grip.
 * - title: a row with no title cannot be identified.
 * - autoplay and lyrics: each already has its own switch (AutoPlay mode,
 *   lyrics_library_col), and a second switch for the same column would let
 *   the two disagree.
 */
import type { ColId } from './library-column-widths';

export const HIDEABLE_COLUMNS = [
	'preview',
	'art',
	'artist',
	'key',
	'bpm',
	'plays',
	'rating',
	'comments',
	'time',
	'quality',
	'energy',
	'genre',
	'stems'
] as const satisfies readonly ColId[];

export type HideableColumn = (typeof HIDEABLE_COLUMNS)[number];

export const HIDEABLE_COLUMN_LABELS: Readonly<Record<HideableColumn, string>> = {
	preview: 'Preview',
	art: 'Artwork',
	artist: 'Artist',
	key: 'Key',
	bpm: 'BPM',
	plays: 'Plays',
	rating: 'Rating',
	comments: 'Comments',
	time: 'Time',
	quality: 'Quality',
	energy: 'Energy',
	genre: 'Genre',
	stems: 'Stems'
};

export function isHideableColumn(value: unknown): value is HideableColumn {
	return typeof value === 'string' && (HIDEABLE_COLUMNS as readonly string[]).includes(value);
}

/** A stored or served hidden-column list, or a thrown Error naming `source`.
 * Duplicates and unknown ids are refused rather than dropped, so a corrupt
 * blob is seen instead of quietly showing a different set of columns. */
export function validateHiddenColumns(value: unknown, source: string): HideableColumn[] {
	if (!Array.isArray(value)) {
		throw new Error(`${source}: library_hidden_columns must be a list, got ${JSON.stringify(value)}`);
	}
	const seen = new Set<HideableColumn>();
	for (const item of value) {
		if (!isHideableColumn(item)) {
			throw new Error(
				`${source}: library_hidden_columns holds ${JSON.stringify(item)}, which is not one of ${HIDEABLE_COLUMNS.join('|')}`
			);
		}
		if (seen.has(item)) {
			throw new Error(`${source}: library_hidden_columns lists ${item} twice`);
		}
		seen.add(item);
	}
	return HIDEABLE_COLUMNS.filter((col) => seen.has(col));
}

/** The hidden list after showing or hiding one column, in table order. */
export function toggleHiddenColumn(
	hidden: readonly HideableColumn[],
	col: HideableColumn
): HideableColumn[] {
	const next = new Set(hidden);
	if (next.has(col)) next.delete(col);
	else next.add(col);
	return HIDEABLE_COLUMNS.filter((c) => next.has(c));
}
