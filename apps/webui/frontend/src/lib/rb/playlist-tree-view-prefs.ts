/**
 * Playlist tree vs column browser layout (issue #3983).
 * Persisted via GET/PUT /api/v1/ui-prefs like library filter prefs.
 */

export type PlaylistTreeViewMode = 'tree' | 'column';

export function validatePlaylistTreeViewField(
	value: unknown,
	storageKey: string
): PlaylistTreeViewMode | undefined {
	if (value === undefined) return undefined;
	if (value !== 'tree' && value !== 'column') {
		throw new Error(
			`${storageKey}: malformed prefs blob (playlist_tree_view must be 'tree'|'column') - ` +
				'clear the localStorage key to recover'
		);
	}
	return value;
}

export interface PlaylistTreeViewPrefsState {
	playlist_tree_view: PlaylistTreeViewMode;
}

export function makePlaylistTreeViewSetters(
	state: PlaylistTreeViewPrefsState,
	persist: () => void,
	syncDiskPrefs: (patch: Partial<PlaylistTreeViewPrefsState>) => void
) {
	function setPlaylistTreeView(next: PlaylistTreeViewMode): void {
		state.playlist_tree_view = next;
		persist();
		syncDiskPrefs({ playlist_tree_view: next });
	}

	function togglePlaylistTreeView(): void {
		setPlaylistTreeView(state.playlist_tree_view === 'tree' ? 'column' : 'tree');
	}

	return { setPlaylistTreeView, togglePlaylistTreeView };
}
