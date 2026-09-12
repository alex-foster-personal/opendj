/** Track context-menu "Show in playlists" copy (LIBM-29). */

export type TrackPlaylistsMenuItem = {
	id: 'show-in-playlists';
	label: string;
	run?: () => void;
};

export const SHOW_IN_PLAYLISTS_LABEL = 'Show in playlists';

export function showInPlaylistsMenuItem(open: () => void): TrackPlaylistsMenuItem {
	return {
		id: 'show-in-playlists',
		label: SHOW_IN_PLAYLISTS_LABEL,
		run: open
	};
}
