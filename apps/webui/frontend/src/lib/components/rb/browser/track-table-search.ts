import { keyForms } from '$lib/rb/key-notation';
import { listingGenre } from '$lib/rb/genre-source';
import type { SearchableTrack } from '$lib/rb/browser-search-query';
import { normalizeRating } from '$lib/rating';

/** Browser row fields consumed by the in-list search grammar (issue #10037). */
export type SearchableBrowserRow = {
	title: string | null;
	artist: string | null;
	comments: string | null;
	key: string | null;
	genre: string | null;
	album?: string | null;
	label?: string | null;
	bpm: number | null;
	rating: number | null;
	rb_meta?: { genre?: string | null } | null;
};

/** Map a table row into the pure search module, resolving genre like filterRows. */
export function searchableTrackFromBrowserRow(row: SearchableBrowserRow): SearchableTrack {
	return {
		title: row.title,
		artist: row.artist,
		comments: row.comments,
		key: row.key,
		key_forms: keyForms(row.key),
		genre: listingGenre(row),
		album: row.album ?? null,
		label: row.label ?? null,
		bpm: row.bpm,
		rating: normalizeRating(row.rating)
	};
}
