/**
 * GENRE-04: the Library shows ONE genre per row, chosen by the configured
 * precedence; the row's `genre_source` names the provider. This turns that id
 * into the Genre cell's hover text.
 */

const LABELS: Record<string, string> = {
	'odj-fused': "Open DJ's inferred genre",
	'library-tag': 'the library tag (rekordbox or file)',
	user: 'your edit',
	beatport: 'Beatport'
};

/** Human label for a provider id; an unknown external id is shown as given. */
export function genreSourceLabel(source: string): string {
	return LABELS[source] ?? source;
}

/** The Genre cell's title: the genre and where it came from, else why it is empty. */
export function genreCellTitle(
	genre: string,
	source: string | null | undefined,
	reason: string | null | undefined
): string | undefined {
	if (!genre) return reason || undefined;
	if (!source) return genre;
	return `${genre} (from ${genreSourceLabel(source)})`;
}

/**
 * Mirrors GENRE_REASON_LIBRARY_TAG_HIDDEN in track_rows.py: the backend sends it
 * when the configured precedence leaves out the library tag and nothing in it
 * has a genre. (private path) pins the two strings together.
 */
export const GENRE_REASON_LIBRARY_TAG_HIDDEN =
	'the genre precedence leaves out the library tag, and no source in it has a genre';

/** The genre a row shows: the listing's choice, else rekordbox's lazily loaded tag unless the precedence hides it. */
export function listingGenre(row: {
	genre?: string | null;
	genre_reason?: string | null;
	rb_meta?: { genre?: string | null } | null;
}): string | null {
	if (row.genre != null) return row.genre;
	if (row.genre_reason === GENRE_REASON_LIBRARY_TAG_HIDDEN) return null;
	return row.rb_meta?.genre ?? null;
}
