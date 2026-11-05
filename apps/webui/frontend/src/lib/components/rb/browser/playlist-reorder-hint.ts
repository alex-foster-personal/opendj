/** W33 (LIBM-182): say why drag-to-reorder is off in a playlist, and how to get it back.
 *
 * Track drag-reorder only works on an editable playlist shown in membership
 * order: a sort or an in-pane search reorders the rows on screen, so a drop
 * position would not match a membership position (BrowserPanel's
 * `reorderablePane`). Before this, the grip simply vanished and a drag did
 * nothing, with no word about why. */

import type { SortKey } from './browser-sort-ipc';

const SORT_LABELS: Readonly<Record<SortKey, string>> = {
	order: '#',
	plays: 'Plays',
	title: 'Track Title',
	artist: 'Artist',
	key: 'Key',
	bpm: 'BPM',
	rating: 'Rating',
	comments: 'Comments',
	time: 'Time',
	energy: 'Energy',
	genre: 'Genre',
	lyrics: 'Lyrics',
	grid: 'Grid',
	autoplay: 'AutoPlay rank',
	work: 'Work',
	composer: 'Composer',
	ensemble: 'Conductor / Ensemble',
	label: 'Label',
	year: 'Year'
};

/** Why an editable playlist pane cannot be drag-reordered right now, or
 * null when it can (or when it is not an editable playlist at all, where
 * reordering was never on offer). */
export function reorderBlockedReason(
	editable: boolean,
	sortKey: SortKey | null,
	search: string
): string | null {
	if (!editable) return null;
	if (sortKey !== null) return `Sorted by ${SORT_LABELS[sortKey]} - clear the sort to drag-reorder this playlist`;
	if (search.trim() !== '') return 'Search is filtering this playlist - clear it to drag-reorder';
	return null;
}
