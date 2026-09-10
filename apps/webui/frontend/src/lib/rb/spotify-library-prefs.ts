/**
 * Spotify source-panel pin/recent setters (#315). Split out of
 * prefs.svelte.ts the same way level-calibration-prefs.ts splits R/M
 * capture: localStorage only, no disk PUT.
 */
import type { SpotifyLibraryPref } from './prefs-types';
import { prependRecentId, togglePinnedId } from './spotify-playlist-rank';

export interface SpotifyLibraryPrefsState {
	spotify_library: SpotifyLibraryPref;
}

export function makeSpotifyLibrarySetters(
	state: SpotifyLibraryPrefsState,
	persist: () => void
): {
	toggleSpotifyPinned(playlistId: string): void;
	rememberSpotifyRecent(playlistId: string): void;
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

	return { toggleSpotifyPinned, rememberSpotifyRecent };
}
