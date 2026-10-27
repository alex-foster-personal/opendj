import { listingGenre } from './genre-source';
import { displayKey, keyForms, type KeyNotation } from './key-notation';

/** Split `text` into plain / hit spans for in-place find highlight. */
export function highlightSpans(
	text: string,
	query: string
): Array<{ text: string; hit: boolean }> {
	const q = query.trim();
	if (q.length < 3 || text === '') return [{ text, hit: false }];
	const lower = text.toLowerCase();
	const needle = q.toLowerCase();
	const out: Array<{ text: string; hit: boolean }> = [];
	let i = 0;
	while (i < text.length) {
		const at = lower.indexOf(needle, i);
		if (at < 0) {
			out.push({ text: text.slice(i), hit: false });
			break;
		}
		if (at > i) out.push({ text: text.slice(i, at), hit: false });
		out.push({ text: text.slice(at, at + needle.length), hit: true });
		i = at + needle.length;
	}
	return out.length > 0 ? out : [{ text, hit: false }];
}

/** True when any common browser field contains the find query (3+ chars). */
export function rowMatchesFind(
	row: {
		title: string | null;
		artist: string | null;
		comments: string | null;
		key: string | null;
		genre: string | null;
		genre_reason?: string | null;
		rb_meta?: { genre: string | null } | null;
	},
	query: string
): boolean {
	const q = query.trim().toLowerCase();
	if (q.length < 3) return false;
	const genre = listingGenre(row);
	return [row.title, row.artist, row.comments, ...keyForms(row.key), genre].some(
		(field) => field !== null && field.toLowerCase().includes(q)
	);
}

/** Find-highlight spans for a key cell, which shows the key in `notation`.
 * A query that matches the key in ANOTHER notation (find "8A" while the cell
 * reads "Am") marks the whole shown key, so a row that matched never looks
 * unmatched (Sol review, PR #7483). Same 3-character floor as highlightSpans. */
export function keyCellSpans(
	raw: string | null,
	notation: KeyNotation,
	query: string
): Array<{ text: string; hit: boolean }> {
	const shown = displayKey(raw, notation)?.text ?? '';
	const spans = highlightSpans(shown, query);
	if (spans.some((span) => span.hit)) return spans;
	const q = query.trim().toLowerCase();
	if (q.length < 3 || shown === '') return spans;
	return keyForms(raw).some((form) => form.toLowerCase().includes(q))
		? [{ text: shown, hit: true }]
		: spans;
}
