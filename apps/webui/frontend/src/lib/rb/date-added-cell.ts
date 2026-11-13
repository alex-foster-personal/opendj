/** LIBUX-13803: the track table's Date Added cell and its sort value.
 *
 * Mirrors rekordbox's Date Added column: YYYY-MM-DD, sortable, hideable, and
 * its first header click sorts newest first (mimicked). The server sends the
 * day already formatted; a row with no added day renders an empty cell and
 * sorts after every dated row in both directions (sortRows keeps nulls last). */

type DateAddedRow = { date_added?: string | null };

/** The text a Date Added cell shows: the YYYY-MM-DD day, or '' when the row has none. */
export function dateAddedCellText(row: DateAddedRow): string {
	return row.date_added ?? '';
}

/** The Date Added sort value: the YYYY-MM-DD day, or null when it is missing or blank. */
export function dateAddedSortValue(row: DateAddedRow): string | null {
	const day = row.date_added ?? '';
	return day.trim() === '' ? null : day;
}
