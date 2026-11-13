/** LIBUX-13845: the track table's Last Played cell and its sort value.
 *
 * Mirrors the last play date rekordbox shows in its history-driven views: date
 * only (YYYY-MM-DD, local calendar day), newest first on the first header click
 * (mimicked). The server sends `last_played_at`, the later of the imported last
 * play and the newest Open DJ play-log entry, read in one bulk query per page.
 * A never-played row renders an empty cell and sorts last in both directions. */

type LastPlayedRow = { last_played_at?: string | null };

/** The Last Played sort value: epoch ms of the last play, or null when never (or unreadably) played. */
export function lastPlayedSortValue(row: LastPlayedRow): number | null {
	const at = row.last_played_at ?? '';
	if (at.trim() === '') return null;
	const ms = Date.parse(at);
	return Number.isFinite(ms) ? ms : null;
}

/** The text a Last Played cell shows: the local YYYY-MM-DD day of the last play, or ''. */
export function lastPlayedCellText(row: LastPlayedRow): string {
	const ms = lastPlayedSortValue(row);
	if (ms === null) return '';
	const day = new Date(ms);
	const pad = (n: number) => String(n).padStart(2, '0');
	return `${day.getFullYear()}-${pad(day.getMonth() + 1)}-${pad(day.getDate())}`;
}
