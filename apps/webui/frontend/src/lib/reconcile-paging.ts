/**
 * Paging and honest wording for the Missing tracks page.
 *
 * The page used to ask the daemon for every broken track in one request
 * (7,127 rows, 3.9 MB, 5 to 9 s on a 9,713-track library) behind a bare
 * "Loading...". It now loads one page, says how many rows it holds against
 * the whole count, and tells a failed load apart from an empty one.
 *
 * Kept free of Svelte and of the API client so the rules are unit-testable.
 */

/** Rows fetched by the first load and by each "show more". */
export const MISSING_PAGE_SIZE = 200;
/** The route rejects a larger `limit`; a reload is split into requests of this size. */
export const MISSING_MAX_REQUEST = 1000;

export interface BrokenPage<T> {
	total: number;
	tracks: T[];
	next_offset?: number | null;
}

export interface BrokenRows<T> {
	tracks: T[];
	total: number;
	nextOffset: number | null;
}

/**
 * Fetch the first `want` rows of the listing, in as few requests as the
 * route's per-request cap allows. Used with `want = MISSING_PAGE_SIZE` on
 * open, and with the currently loaded count after a relocate or a removal so
 * an edit never collapses what the user had already paged in.
 */
export async function loadBrokenRows<T>(
	fetchPage: (limit: number, offset: number) => Promise<BrokenPage<T>>,
	want: number,
	startOffset = 0
): Promise<BrokenRows<T>> {
	if (!Number.isInteger(want) || want <= 0) {
		throw new Error(`loadBrokenRows: want must be a positive integer, got ${want}`);
	}
	const tracks: T[] = [];
	let total = 0;
	let offset: number | null = startOffset;
	while (offset !== null && tracks.length < want) {
		const limit = Math.min(MISSING_MAX_REQUEST, want - tracks.length);
		const page: BrokenPage<T> = await fetchPage(limit, offset);
		total = page.total;
		tracks.push(...page.tracks);
		const next: number | null = page.next_offset ?? null;
		if (next !== null && next <= offset) {
			throw new Error(
				`loadBrokenRows: the listing did not advance (offset ${offset}, next_offset ${next})`
			);
		}
		offset = next;
	}
	return { tracks, total, nextOffset: offset };
}

/** Which of the page's mutually exclusive states is showing. */
export type MissingView = 'loading' | 'error' | 'empty' | 'rows';

/**
 * A failed load is its own state. It must never fall through to "empty",
 * which reads as "the library is clean" when nothing was measured.
 */
export function missingView(state: {
	loading: boolean;
	error: string | null;
	loaded: number;
}): MissingView {
	if (state.loaded > 0) return 'rows';
	if (state.loading) return 'loading';
	if (state.error !== null) return 'error';
	return 'empty';
}

const count = (n: number): string => n.toLocaleString('en-US');
const noun = (n: number): string => (n === 1 ? 'missing track' : 'missing tracks');

export function missingCountLabel(loaded: number, total: number): string {
	if (loaded >= total) return `${count(total)} ${noun(total)}`;
	return `Showing ${count(loaded)} of ${count(total)} ${noun(total)}`;
}

/**
 * Hover text for the count: what is counted, when, and what it is not.
 * The figure is deliberately not quoted against the whole library: rows on a
 * drive that is not plugged in are counted here too, so a share of the
 * library would mix "gone" with "away" (docs/library-availability.md).
 */
export function missingCountTitle(total: number, measuredAt: Date): string {
	const at = measuredAt.toLocaleTimeString('en-GB', { hour12: false });
	return (
		`${count(total)} local library rows whose recorded local path does not resolve on this ` +
		`machine, measured at ${at}. This is a count of rows, not a share of the library: it ` +
		`includes rows on a drive that is not plugged in, and it leaves out streaming rows and ` +
		`rows with no recorded path.`
	);
}

export function missingMoreLabel(loaded: number, total: number): string {
	return `Show ${count(Math.min(MISSING_PAGE_SIZE, Math.max(0, total - loaded)))} more`;
}
