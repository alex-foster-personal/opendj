/** Track context-menu add-to-playlist entry (LIBM-95). */

export const ADD_TO_PLAYLIST_TITLE = 'POST /api/v1/playlists/{playlist_id}/items:add';
export const ADD_TO_PLAYLIST_EMPTY_TITLE = 'select at least one track first';

export type AddToPlaylistMenuItem = {
	id: 'add-playlist';
	label: 'Add to playlist...';
	run?: () => void;
	title?: string;
};

export function addToPlaylistMenuItem(
	selectedIds: string[],
	openPicker: ((ids: string[]) => void) | undefined
): AddToPlaylistMenuItem {
	if (openPicker && selectedIds.length > 0) {
		return {
			id: 'add-playlist',
			label: 'Add to playlist...',
			run: () => openPicker(selectedIds),
			title: ADD_TO_PLAYLIST_TITLE
		};
	}
	return {
		id: 'add-playlist',
		label: 'Add to playlist...',
		title: ADD_TO_PLAYLIST_EMPTY_TITLE
	};
}
