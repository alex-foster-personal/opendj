/**
 * Pure hide-broken playlist predicates (issue #3534).
 *
 * Threshold is server-owned (GET /api/v1/settings); callers pass the hydrated
 * min playable-track count. No Svelte runes so unit tests can import this file.
 */

export type PlaylistAvailability = {
	available_count: number;
	track_count: number;
};

export type PlaylistWithId = PlaylistAvailability & { playlist_id: string };

/**
 * True when the Broken filter should hide or dim this playlist.
 *
 * Policy (issue #3534): hide when playable count is below the server floor.
 * Replaces the old ratio rule (available_count / track_count < 0.3) which hid
 * large playlists that still had many usable tracks.
 */
export function playlistMostlyBroken(
	availableCount: number,
	minVisibleTracks: number
): boolean {
	if (availableCount < 0) return false;
	return availableCount < minVisibleTracks;
}

export function isPlaylistVisibleWithBrokenFilter(
	playlist: PlaylistAvailability,
	hideBrokenLinks: boolean,
	minVisibleTracks: number,
	withinCreateGrace: boolean
): boolean {
	if (!hideBrokenLinks) return true;
	if (withinCreateGrace) return true;
	return !playlistMostlyBroken(playlist.available_count, minVisibleTracks);
}

export function countHiddenBrokenPlaylists(
	playlists: PlaylistWithId[],
	hideBrokenLinks: boolean,
	minVisibleTracks: number,
	isWithinCreateGrace: (playlistId: string) => boolean
): number {
	if (!hideBrokenLinks) return 0;
	return playlists.filter(
		(p) =>
			!isWithinCreateGrace(p.playlist_id) &&
			playlistMostlyBroken(p.available_count, minVisibleTracks)
	).length;
}
