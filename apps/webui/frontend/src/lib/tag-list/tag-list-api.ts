/**
 * The Tag List prep list over HTTP (issue #10028), rekordbox Tag List parity.
 *
 * Hand-written and typed to the shapes of `apps/webui/server/routes/taglist.py`
 * (`TagListOut`, `TagListAddOut`, `TagListClearOut`, `TagListSaveOut`). The routes
 * land in PR #14510, which is not in this branch's base, so the generated
 * `api-types.ts` does not carry them yet. Pure: no Svelte, no DOM.
 */
import { API_BASE } from '$lib/api/base';
import { apiErrorFrom } from '$lib/api/errors';

export interface TagListItem {
	/** 1-based order in the Tag List. */
	position: number;
	stable_id: string;
	title: string | null;
}

export interface TagList {
	count: number;
	/** Most tracks the Tag List holds (100, rekordbox's cap). */
	cap: number;
	items: TagListItem[];
}

export interface TagListAdded extends TagList {
	/** Ids newly appended; ids already listed are ignored. */
	added: string[];
}

export interface TagListSaved {
	playlist_id: string;
	name: string;
	track_count: number;
}

const JSON_HEADERS = { Accept: 'application/json', 'Content-Type': 'application/json' };

async function readJson<T>(response: Response): Promise<T> {
	if (!response.ok) throw await apiErrorFrom(response);
	return (await response.json()) as T;
}

/** GET /taglist: the Tag List in order, with its count and cap. */
export async function fetchTagList(fetchImpl: typeof fetch = fetch): Promise<TagList> {
	const response = await fetchImpl(`${API_BASE}/api/v1/taglist`, {
		headers: { Accept: 'application/json' }
	});
	return readJson<TagList>(response);
}

/** POST /taglist/items: append tracks; a 409 TAGLIST_FULL refuses an add past the cap whole. */
export async function addToTagList(
	stableIds: readonly string[],
	fetchImpl: typeof fetch = fetch
): Promise<TagListAdded> {
	if (stableIds.length === 0) throw new Error('tag list: add needs at least one stable_id');
	const response = await fetchImpl(`${API_BASE}/api/v1/taglist/items`, {
		method: 'POST',
		headers: JSON_HEADERS,
		body: JSON.stringify({ stable_ids: [...new Set(stableIds)] })
	});
	return readJson<TagListAdded>(response);
}

/** DELETE /taglist/items/{stable_id}: take one track off the Tag List. */
export async function removeFromTagList(
	stableId: string,
	fetchImpl: typeof fetch = fetch
): Promise<TagList> {
	const response = await fetchImpl(
		`${API_BASE}/api/v1/taglist/items/${encodeURIComponent(stableId)}`,
		{ method: 'DELETE', headers: { Accept: 'application/json' } }
	);
	return readJson<TagList>(response);
}

/** DELETE /taglist: empty the Tag List; answers how many tracks it held. */
export async function clearTagList(fetchImpl: typeof fetch = fetch): Promise<{ removed: number }> {
	const response = await fetchImpl(`${API_BASE}/api/v1/taglist`, {
		method: 'DELETE',
		headers: { Accept: 'application/json' }
	});
	return readJson<{ removed: number }>(response);
}

/** POST /taglist:save: a normal playlist of the Tag List in order; the Tag List stays. */
export async function saveTagListAsPlaylist(
	name: string,
	fetchImpl: typeof fetch = fetch
): Promise<TagListSaved> {
	const response = await fetchImpl(`${API_BASE}/api/v1/taglist:save`, {
		method: 'POST',
		headers: JSON_HEADERS,
		body: JSON.stringify({ name })
	});
	return readJson<TagListSaved>(response);
}
