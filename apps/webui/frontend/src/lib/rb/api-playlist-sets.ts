/**
 * Typed client for playlist-set routes (SET-05).
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
	name: string
): Promise<PlaylistSet> {
	try {
		return (await unwrap(
			api.POST('/api/v1/playlists/{playlist_id}/sets', {
				params: { path: { playlist_id: playlistId } },
				body: { name }
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
