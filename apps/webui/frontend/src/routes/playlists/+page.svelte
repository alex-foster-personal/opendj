<script lang="ts">
	/**
	 * ARTTHUMB-8728d: every playlist as a card (232 px art, name, track count),
	 * Spotify's "Public Playlists" grid. A lazy route, charged to its own
	 * playlists-grid bundle bucket. Agent parity: GET /api/v1/playlists plus
	 * GET /api/v1/playlists/{id}/thumbnail?size=232 (opendj playlist thumbnail).
	 */
	import { onMount } from 'svelte';
	import PlaylistsGridBrowser from '$lib/components/rb/browser/PlaylistsGridBrowser.svelte';
	import { listPlaylists, type PlaylistSummary } from '$lib/api';
	import { subscribeKind, subscribeResync } from '$lib/api/events-bus';
	import { artThumbnailsAvailable } from '$lib/rb/library-sidebar-art.svelte';
	import { coalesce } from '$lib/rb/coalesce';

	let playlists = $state<PlaylistSummary[] | null>(null);
	let loadError = $state<string | null>(null);

	async function refreshPlaylists(): Promise<void> {
		try {
			playlists = await listPlaylists();
			loadError = null;
		} catch (err: unknown) {
			loadError = err instanceof Error ? err.message : String(err);
		}
	}

	const requestRefresh = coalesce(refreshPlaylists);

	onMount(() => {
		void requestRefresh();
		const unkind = subscribeKind('playlists', () => void requestRefresh());
		const unresync = subscribeResync(() => void requestRefresh());
		return () => {
			unkind();
			unresync();
		};
	});
</script>

<svelte:head><title>Playlists - Open DJ</title></svelte:head>

<main class="playlists-grid-page" data-testid="playlists-grid-page">
	<h1 title="Every playlist as a card with its art">Playlists</h1>
	<PlaylistsGridBrowser artThumbnailsOn={artThumbnailsAvailable()} {playlists} {loadError} />
</main>

<style>
	.playlists-grid-page {
		padding: 24px;
		color: var(--rb-text);
		background: var(--rb-bg);
		min-height: 100vh;
		box-sizing: border-box;
	}
	h1 {
		font-size: 22px;
		margin: 0 0 16px;
	}
</style>
