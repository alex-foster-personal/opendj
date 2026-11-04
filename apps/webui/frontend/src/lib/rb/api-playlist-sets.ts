/**
 * Typed client for playlist-set routes (SET-05) and set routes (SET-6586).
 */
import { RbApiError } from './api-rb-error';
import { ApiError, api, unwrap } from '../api/client';

export interface PlaylistSetEntry {
	stable_id: string;
	position: number;
}

export interface PlaylistSet {
	id: number;
	playlist_id: string;
	name: string;
	play_count: number;
	entries: PlaylistSetEntry[];
	/** SET-6586 route: component ids in slot order; [] for a flat SET-05 set. */
	parts: number[];
	created_at: string;
	updated_at: string;
}

export interface PlaylistSetRunResult {
	set_id: number;
	kind: 'practice' | 'performance';
	play_count: number;
}

function _throwPlaylistSetError(error: unknown): never {
	if (error instanceof ApiError) {
		throw new RbApiError(error.status, error.code, error.message);
	}
	throw error;
}

export async function listPlaylistSets(playlistId: string): Promise<PlaylistSet[]> {
	try {
		const data = (await unwrap(
			api.GET('/api/v1/playlists/{playlist_id}/sets', {
				params: { path: { playlist_id: playlistId } }
			})
		)) as { sets: PlaylistSet[] };
		return data.sets;
	} catch (error) {
		_throwPlaylistSetError(error);
	}
}

export async function createPlaylistSet(
	playlistId: string,
	name: string,
	parts?: number[]
): Promise<PlaylistSet> {
	try {
		return (await unwrap(
			api.POST('/api/v1/playlists/{playlist_id}/sets', {
				params: { path: { playlist_id: playlistId } },
				body: parts && parts.length > 0 ? { name, parts } : { name }
			})
		)) as PlaylistSet;
	} catch (error) {
		_throwPlaylistSetError(error);
	}
}

export async function createPlaylistSetRun(
	playlistId: string,
	setId: number,
	kind: 'practice' | 'performance'
): Promise<PlaylistSetRunResult> {
	try {
		return (await unwrap(
			api.POST('/api/v1/playlists/{playlist_id}/sets/{set_id}/runs', {
				params: { path: { playlist_id: playlistId, set_id: setId } },
				body: { kind }
			})
		)) as PlaylistSetRunResult;
	} catch (error) {
		_throwPlaylistSetError(error);
	}
}

export async function updatePlaylistSetParts(
	playlistId: string,
	setId: number,
	parts: number[]
): Promise<PlaylistSet> {
	try {
		return (await unwrap(
			api.PUT('/api/v1/playlists/{playlist_id}/sets/{set_id}/parts', {
				params: { path: { playlist_id: playlistId, set_id: setId } },
				body: { parts }
			})
		)) as PlaylistSet;
	} catch (error) {
		_throwPlaylistSetError(error);
	}
}

export interface PlaylistComponent {
	id: number;
	playlist_id: string;
	name: string;
	slot: string;
	stable_ids: string[];
	tags: string[];
	created_at: string;
	updated_at: string;
	/** Another playlist's component that a saved set borrows: drawn in the route view, never edited here. */
	borrowed?: boolean;
}

export interface PlaylistRoute {
	parts: number[];
	stable_ids: string[];
	set_id: number | null;
	repeats: string[];
}

export interface PlaylistRouteList {
	routes: PlaylistRoute[];
	total: number;
	truncated: boolean;
}

export async function listPlaylistComponents(playlistId: string): Promise<PlaylistComponent[]> {
	try {
		const data = (await unwrap(
			api.GET('/api/v1/playlists/{playlist_id}/components', {
				params: { path: { playlist_id: playlistId } }
			})
		)) as { components: PlaylistComponent[] };
		return data.components;
	} catch (error) {
		_throwPlaylistSetError(error);
	}
}

export async function createPlaylistComponent(
	playlistId: string,
	body: { name: string; slot: string; stable_ids: string[]; tags: string[] }
): Promise<PlaylistComponent> {
	try {
		return (await unwrap(
			api.POST('/api/v1/playlists/{playlist_id}/components', {
				params: { path: { playlist_id: playlistId } },
				body
			})
		)) as PlaylistComponent;
	} catch (error) {
		_throwPlaylistSetError(error);
	}
}

export async function listPlaylistRoutes(
	playlistId: string,
	limit?: number,
	offset?: number
): Promise<PlaylistRouteList> {
	try {
		return (await unwrap(
			api.GET('/api/v1/playlists/{playlist_id}/routes', {
				params: {
					path: { playlist_id: playlistId },
					query: { ...(limit === undefined ? {} : { limit }), ...(offset === undefined ? {} : { offset }) }
				}
			})
		)) as PlaylistRouteList;
	} catch (error) {
		_throwPlaylistSetError(error);
	}
}

