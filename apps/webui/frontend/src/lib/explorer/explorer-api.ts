/**
 * The Explorer source over HTTP (issue #9747), rekordbox Explorer parity (mimicked).
 *
 * Hand-written and typed to the shapes of `apps/webui/server/routes/library_folders.py`
 * (`FolderTreeOut`, `FolderNodeOut`, `FolderTracksOut`). The routes land in PR #14606,
 * which is not in this branch's base, so the generated `api-types.ts` does not carry
 * them yet. Pure: no Svelte, no DOM.
 */
import { API_BASE } from '$lib/api/base';
import { apiErrorFrom } from '$lib/api/errors';

export interface FolderNode {
	name: string;
	/** The folder's full path, exactly as the tracks route takes it back. */
	path: string;
	/** Tracks in this folder and every folder below it. */
	track_count: number;
	/** Tracks directly in this folder. */
	direct_track_count: number;
	children: FolderNode[];
}

export interface FolderTree {
	roots: FolderNode[];
	track_count: number;
	/** Tracks with no stored file path; they appear in no folder. */
	unlocated_count: number;
}

export interface FolderTracks {
	path: string;
	recursive: boolean;
	track_count: number;
	/** File-name order, then (recursive) each child folder's in turn. */
	items: { stable_id: string; file_path: string }[];
}

async function readJson<T>(response: Response): Promise<T> {
	if (!response.ok) throw await apiErrorFrom(response);
	return (await response.json()) as T;
}

/** GET /library/folders: the folder tree of every track's stored path, with counts. */
export async function fetchFolderTree(fetchImpl: typeof fetch = fetch): Promise<FolderTree> {
	const response = await fetchImpl(`${API_BASE}/api/v1/library/folders`, {
		headers: { Accept: 'application/json' }
	});
	return readJson<FolderTree>(response);
}

/** GET /library/folders/tracks: one folder's tracks (or every track under it); 404 when none live there. */
export async function fetchFolderTracks(
	path: string,
	recursive: boolean,
	fetchImpl: typeof fetch = fetch
): Promise<FolderTracks> {
	if (path === '') throw new Error('explorer: a folder path is required');
	const query = new URLSearchParams({ path, recursive: String(recursive) });
	const response = await fetchImpl(`${API_BASE}/api/v1/library/folders/tracks?${query}`, {
		headers: { Accept: 'application/json' }
	});
	return readJson<FolderTracks>(response);
}
