/** LIBUX-13747: the track table's Album cell and its sort value.
 *
 * Mirrors rekordbox's Album column: plain text, sortable, hideable. A row with
 * no album renders an empty cell and sorts after every titled album in both
 * directions (sortRows keeps nulls last). */

type AlbumRow = { album?: string | null };

/** The text an Album cell shows: the album, or '' when the row has none. */
export function albumCellText(row: AlbumRow): string {
	return row.album ?? '';
}

/** The Album sort value: the album, or null when it is missing or blank. */
export function albumSortValue(row: AlbumRow): string | null {
	const album = row.album ?? '';
	return album.trim() === '' ? null : album;
}
