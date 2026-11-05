/**
 * LIBM-189: store cover suggestions (Deezer / iTunes) wait on the user's consent.
 *
 * Pure helpers for the review dialog: the enrich card line, when the dialog
 * opens by itself, and the decide request each button sends
 * (POST /api/v1/artwork/suggestions/decide; CLI `opendj artwork`, MCP
 * `artwork_suggestions_decide`).
 */

export type ArtworkSuggestion = {
	stable_id: string;
	source: string;
	source_label: string;
	cover_url: string;
	hit_title: string;
	hit_artist: string;
	track_title: string;
	track_artist: string;
	image_url: string;
};

export type SuggestionsDoc = { note: string; pending: ArtworkSuggestion[] };

export type DecideBody = {
	accept: string[];
	skip: string[];
	accept_all: boolean;
	skip_all: boolean;
};

/** The enrich card line, or null when nothing waits. */
export function suggestionsLine(count: number): string | null {
	if (count <= 0) return null;
	return `${count} ${count === 1 ? 'cover was' : 'covers were'} found on Deezer/iTunes and wait for your OK`;
}

/**
 * The dialog opens by itself once a session, only while no deck plays: it
 * never lands on top of a live set, and it never holds analysis (that runs in
 * the engine regardless).
 *
 *   [if] covers wait, no deck plays and it has not opened this session [then] it opens
 *   [if] a deck plays [then] it waits
 *   [if] it already opened this session [then] only the card's Review button opens it
 */
export function shouldAutoOpen(
	count: number,
	playing: boolean,
	shownThisSession: boolean
): boolean {
	return count > 0 && !playing && !shownThisSession;
}

/**
 * The request each button sends. Save selected and Skip act on the ticked
 * covers only; the unticked ones stay pending. Save all takes every pending one.
 */
export function decideBody(
	action: 'save-selected' | 'save-all' | 'skip',
	selected: ReadonlySet<string>
): DecideBody {
	const none: DecideBody = { accept: [], skip: [], accept_all: false, skip_all: false };
	if (action === 'save-all') return { ...none, accept_all: true };
	if (action === 'save-selected') return { ...none, accept: [...selected] };
	return { ...none, skip: [...selected] };
}
