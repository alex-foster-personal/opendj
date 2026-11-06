/**
 * CMDK-6848: a pending request to bring one library playlist's tree row into
 * view. LibraryNav answers it by switching to its Playlists tab; PlaylistTree
 * answers it by opening its Playlists section, scrolling the row into view,
 * and clearing the request.
 */
export const playlistReveal = $state<{ id: string | null }>({ id: null });

export function revealPlaylist(id: string): void {
	playlistReveal.id = id;
}
