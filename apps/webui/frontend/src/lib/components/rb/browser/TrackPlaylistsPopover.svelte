<script lang="ts">
	import { onMount } from 'svelte';
	import { ApiError, readApiErrorStatus } from '$lib/api/errors';
	import { listTrackPlaylists, type TrackPlaylistHit } from '$lib/rb/track-playlists';
	import { runPerformanceCommandFromUi } from '$lib/rb/performance-ipc.svelte';
	import TrackPlaylistsPopoverView from './TrackPlaylistsPopoverView.svelte';

	let { stableId, x, y, onclose }: {
		stableId: string;
		x: number;
		y: number;
		onclose: () => void;
	} = $props();

	let loading = $state(true);
	let hits = $state<TrackPlaylistHit[]>([]);
	let errorMessage = $state<string | null>(null);

	onMount(() => {
		void (async () => {
			try {
				hits = await listTrackPlaylists(stableId);
			} catch (error) {
				if (readApiErrorStatus(error) === 404) {
					errorMessage = 'Track not found';
				} else if (error instanceof ApiError) {
					errorMessage = error.message;
				} else if (error instanceof Error) {
					errorMessage = error.message;
				} else {
					errorMessage = 'Failed to load playlists';
				}
			} finally {
				loading = false;
			}
		})();
	});

	async function selectPlaylist(playlistId: string): Promise<void> {
		onclose();
		await runPerformanceCommandFromUi({ type: 'browser_select_playlist', playlist_id: playlistId });
	}
</script>

<TrackPlaylistsPopoverView
	{x}
	{y}
	{loading}
	{errorMessage}
	{hits}
	onselect={(playlistId) => void selectPlaylist(playlistId)}
	{onclose}
/>
