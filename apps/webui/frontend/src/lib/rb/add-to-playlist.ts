/** Compose helper for the add-to-playlist picker (LIBM-95). */

import type { PlaylistNode } from '$lib/rb/library-types';
import { getPlaylistTracksEtag, transferPlaylistTracks } from '$lib/rb/playlist-write';

export const PLAYLIST_PICKER_SEARCH_THRESHOLD = 15;

export async function appendTracksToPlaylist(
	playlistId: string,
	stableIds: string[]
): Promise<void> {
	if (stableIds.length === 0) {
		throw new Error('select at least one track first');
	}
	const dest = await getPlaylistTracksEtag(playlistId);
	await transferPlaylistTracks(playlistId, dest.etag, {
		stable_ids: stableIds,
		mode: 'add'
	});
}

export function addToPlaylistToastMessage(count: number, playlistName: string): string {
	if (count === 1) return `Added 1 track to "${playlistName}"`;
	return `Added ${count} tracks to "${playlistName}"`;
}

export function writablePlaylistNodes(nodes: readonly PlaylistNode[]): PlaylistNode[] {
	return nodes.filter((n) => n.kind === 'playlist');
}

export function filterPlaylistsByName(nodes: readonly PlaylistNode[], query: string): PlaylistNode[] {
	const q = query.trim().toLowerCase();
	if (q === '') return [...nodes];
	return nodes.filter((n) => n.name.toLowerCase().includes(q));
}
