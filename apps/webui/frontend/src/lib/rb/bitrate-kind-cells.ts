/** LIBUX-13824: the track table's Bitrate and Kind cells and their sort values.
 *
 * Mirrors rekordbox's Bitrate (`320 kbps`) and Kind (MP3, FLAC, WAV, AIFF, M4A)
 * browser columns (mimicked). Both read the row's existing quality data
 * (`quality.kbps`, `quality.container`); a row with no value renders an empty
 * cell and sorts after every known value in both directions (sortRows keeps
 * nulls last). */

type QualityRow = { quality?: { kbps: number | null; container: string } | null };

/** The text a Bitrate cell shows: `<n> kbps`, or '' when the bitrate is unknown. */
export function bitrateCellText(row: QualityRow): string {
	const kbps = bitrateSortValue(row);
	return kbps === null ? '' : `${kbps} kbps`;
}

/** The Bitrate sort value: kbps as a number, or null when it is unknown. */
export function bitrateSortValue(row: QualityRow): number | null {
	const kbps = row.quality?.kbps;
	return typeof kbps === 'number' && Number.isFinite(kbps) ? kbps : null;
}

/** The text a Kind cell shows: the upper-case container without its dot, e.g. 'FLAC', or ''. */
export function kindCellText(row: QualityRow): string {
	return kindSortValue(row) ?? '';
}

/** The Kind sort value: the upper-case container, or null when the row has none. */
export function kindSortValue(row: QualityRow): string | null {
	const kind = (row.quality?.container ?? '').trim().replace(/^\./, '').toUpperCase();
	return kind === '' ? null : kind;
}
