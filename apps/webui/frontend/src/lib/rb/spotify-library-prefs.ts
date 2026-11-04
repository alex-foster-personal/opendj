/**
 * Spotify source-panel pin/recent setters (#315). Split out of
 * prefs.svelte.ts the same way level-calibration-prefs.ts splits R/M
 * capture. Pins and recents stay in localStorage; the shrink choice is also
 * written to ui-prefs, so PUT /api/v1/ui-prefs reads and changes it too.
 */
import type { SpotifyLibraryPref } from './prefs-types';
import { prependRecentId, togglePinnedId } from './spotify-playlist-rank';

export interface SpotifyLibraryPrefsState {
	spotify_library: SpotifyLibraryPref;
}

export function makeSpotifyLibrarySetters(
	state: SpotifyLibraryPrefsState,
	persist: () => void,
	sync: (patch: { spotify_shrink_main_for_player: boolean }) => void
): {
	toggleSpotifyPinned(playlistId: string): void;
	rememberSpotifyRecent(playlistId: string): void;
	setSpotifyShrinkMain(shrink: boolean): void;
} {
	function toggleSpotifyPinned(playlistId: string): void {
		const next = togglePinnedId(state.spotify_library.pinned_ids, playlistId);
		if (next === state.spotify_library.pinned_ids) return;
		state.spotify_library = { ...state.spotify_library, pinned_ids: next };
		persist();
	}

	function rememberSpotifyRecent(playlistId: string): void {
		const next = prependRecentId(state.spotify_library.recent_ids, playlistId);
		if (next === state.spotify_library.recent_ids) return;
		state.spotify_library = { ...state.spotify_library, recent_ids: next };
		persist();
	}

	function setSpotifyShrinkMain(shrink: boolean): void {
		if (state.spotify_library.shrink_main_for_player === shrink) return;
		state.spotify_library = { ...state.spotify_library, shrink_main_for_player: shrink };
		persist();
		sync({ spotify_shrink_main_for_player: shrink });
	}

	return { toggleSpotifyPinned, rememberSpotifyRecent, setSpotifyShrinkMain };
}
