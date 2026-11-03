<script lang="ts">
	import type { PlaylistSummaryHydrated } from '$lib/rb/api-rb';
	import { toggleSpotifyPinned, uiPrefs } from '$lib/rb/prefs.svelte';
	import { SPOTIFY_PURCHASE_SOURCES, type SpotifyPendingTrack } from '$lib/rb/spotify-api';
	import SpotifySourcePanelView from './SpotifySourcePanelView.svelte';

	let {
		playlists,
		playlistsLoading,
		playlistsError,
		selectedId,
		pendingTracks,
		loading,
		error,
		oncollection,
		onselect
	}: {
		playlists: PlaylistSummaryHydrated[];
		playlistsLoading: boolean;
		playlistsError: string | null;
		selectedId: string | null;
		pendingTracks: SpotifyPendingTrack[] | null;
		loading: boolean;
		error: string | null;
		oncollection: () => void;
		onselect: (playlist: PlaylistSummaryHydrated) => void;
	} = $props();
</script>

<SpotifySourcePanelView
	{playlists}
	{playlistsLoading}
	{playlistsError}
	{selectedId}
	{pendingTracks}
	{loading}
	{error}
	pinnedIds={uiPrefs.spotify_library.pinned_ids}
	recentIds={uiPrefs.spotify_library.recent_ids}
	purchaseSources={SPOTIFY_PURCHASE_SOURCES}
	{oncollection}
	{onselect}
	ontogglepin={toggleSpotifyPinned}
/>
