/**
 * Soft-deleted playlist lifecycle client (LIBMX-03).
 *
 * Kept out of api-rb.ts (single-owner hotspot per FANOUT-CONVENTIONS.md).
 */

import { api, unwrap } from '../api/client';
import type { components } from '../api-types';

export type DeletedPlaylistOut = components['schemas']['DeletedPlaylistOut'];
export type PlaylistRowWire = components['schemas']['PlaylistWriteOut'];

export async function listDeletedPlaylists(): Promise<DeletedPlaylistOut[]> {
	return await unwrap(api.GET('/api/v1/playlists/deleted'));
}

export async function undeletePlaylist(playlistId: string): Promise<PlaylistRowWire> {
	return await unwrap(
		api.POST('/api/v1/playlists/{playlist_id}:undelete', {
			params: { path: { playlist_id: playlistId } }
		})
	);
}
