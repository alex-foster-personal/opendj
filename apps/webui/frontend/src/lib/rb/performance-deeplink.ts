/**
 * Pure serializers for /performance URL deep links (lv1 playlist, lv2 deck ids).
 * No DOM, no runes - unit-tested directly.
 */

export type DeeplinkSource = 'collection' | 'spotify';

export interface PerformanceDeeplinkLv1 {
	source: DeeplinkSource;
	playlist_id: string | null;
}

export type DeckId = 1 | 2 | 3 | 4;

const DECK_QUERY_KEYS: Record<DeckId, string> = { 1: 'd1', 2: 'd2', 3: 'd3', 4: 'd4' };
const DECK_IDS: DeckId[] = [1, 2, 3, 4];

function _asSearchParams(search: string | URLSearchParams): URLSearchParams {
	if (typeof search === 'string') {
		const trimmed = search.startsWith('?') ? search.slice(1) : search;
		return new URLSearchParams(trimmed);
	}
	return search;
}

export function parseLv1(search: string | URLSearchParams): PerformanceDeeplinkLv1 {
	const params = _asSearchParams(search);
	const sourceParam = params.get('source');
	const playlist = params.get('playlist');
	const playlist_id = playlist !== null && playlist.length > 0 ? playlist : null;
	if (sourceParam === 'spotify') {
		return { source: 'spotify', playlist_id };
	}
	return { source: 'collection', playlist_id };
}

export function writeLv1(params: URLSearchParams, lv1: PerformanceDeeplinkLv1): URLSearchParams {
	const next = new URLSearchParams(params);
	if (lv1.source === 'spotify') {
		next.set('source', 'spotify');
		if (lv1.playlist_id !== null) next.set('playlist', lv1.playlist_id);
		else next.delete('playlist');
		return next;
	}
	next.delete('source');
	if (lv1.playlist_id !== null) next.set('playlist', lv1.playlist_id);
	else next.delete('playlist');
	return next;
}

export function parseLv2Ids(search: string | URLSearchParams): Partial<Record<DeckId, string>> {
	const params = _asSearchParams(search);
	const ids: Partial<Record<DeckId, string>> = {};
	for (const deckId of DECK_IDS) {
		const value = params.get(DECK_QUERY_KEYS[deckId]);
		if (value !== null && value.length > 0) ids[deckId] = value;
	}
	return ids;
}

export function writeLv2Ids(
	params: URLSearchParams,
	ids: Partial<Record<DeckId, string>>
): URLSearchParams {
	const next = new URLSearchParams(params);
	for (const deckId of DECK_IDS) {
		const key = DECK_QUERY_KEYS[deckId];
		const value = ids[deckId];
		if (value !== undefined && value.length > 0) next.set(key, value);
		else next.delete(key);
	}
	return next;
}

/** Round-trip lv1 through URLSearchParams for unit tests. */
export function applyToLocation(search: string): string {
	const lv1 = parseLv1(search);
	const params = writeLv1(new URLSearchParams(), lv1);
	const qs = params.toString();
	return qs.length > 0 ? `?${qs}` : '';
}

/** history.replaceState target; URL.search already includes the leading ?. */
export function formatReplaceStateUrl(url: URL): string {
	return `${url.pathname}${url.search}${url.hash}`;
}
