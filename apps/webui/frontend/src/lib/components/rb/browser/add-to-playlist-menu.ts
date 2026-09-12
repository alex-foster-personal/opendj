/** Track context-menu add-to-playlist entry (LIBM-95). */

export type AddToPlaylistMenuItem = {
	id: 'add-playlist';
	label: 'Add to playlist...';
	run?: () => void;
};

export function addToPlaylistMenuItem(
	selectedIds: string[],
	openPicker: ((ids: string[]) => void) | undefined
): AddToPlaylistMenuItem {
	if (openPicker && selectedIds.length > 0) {
		return {
			id: 'add-playlist',
			label: 'Add to playlist...',
			run: () => openPicker(selectedIds)
		};
	}
	return {
		id: 'add-playlist',
		label: 'Add to playlist...'
	};
}
