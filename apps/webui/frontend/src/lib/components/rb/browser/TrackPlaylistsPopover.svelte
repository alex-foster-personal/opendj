<script lang="ts">
	import { onMount, tick } from 'svelte';
	import { pointFloatingAction } from '$lib/ui/clamp-to-viewport';
	import { ApiError, readApiErrorStatus } from '$lib/api/client';
	import { listTrackPlaylists, type TrackPlaylistHit } from '$lib/rb/track-playlists';
	import { runPerformanceCommandFromUi } from '$lib/rb/performance-ipc.svelte';

	let { stableId, x, y, onclose }: {
		stableId: string;
		x: number;
		y: number;
		onclose: () => void;
	} = $props();

	let menu = $state<HTMLDivElement | null>(null);
	let loading = $state(true);
	let hits = $state<TrackPlaylistHit[]>([]);
	let errorMessage = $state<string | null>(null);

	// Placement is pointFloatingAction's job, not a one-shot clamp here: the
	// menu opens on a single loading row and grows once its items arrive, so
	// a clamp measured at open left the grown menu hanging off the viewport
	// bottom (unclickable - a fixed node cannot be scrolled into view). The
	// action re-clamps from (x, y) on every size change.
	async function focusMenu(): Promise<void> {
		await tick();
		menu?.focus();
	}

	$effect(() => {
		void x;
		void y;
		void focusMenu();
	});

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

	function onKeydown(event: KeyboardEvent): void {
		if (event.key === 'Escape') {
			event.preventDefault();
			onclose();
		}
	}

	function onOutsidePointerDown(event: PointerEvent): void {
		if (menu !== null && event.target instanceof Node && !menu.contains(event.target)) onclose();
	}
</script>

<svelte:window onkeydown={onKeydown} onpointerdowncapture={onOutsidePointerDown} />

<div
	bind:this={menu}
	class="track-playlists-menu"
	data-testid="track-playlists-menu"
	role="menu"
	tabindex="-1"
	use:pointFloatingAction={{ x, y }}
>
	{#if loading}
		<button type="button" role="menuitem" disabled>Loading playlists...</button>
	{:else if errorMessage !== null}
		<button type="button" role="menuitem" disabled>{errorMessage}</button>
	{:else if hits.length === 0}
		<button type="button" role="menuitem" disabled>Not in any playlist</button>
	{:else}
		{#each hits as hit (hit.playlist_id)}
			<button type="button" role="menuitem" onclick={() => void selectPlaylist(hit.playlist_id)}>{hit.name}</button>
		{/each}
	{/if}
</div>

<style>
	.track-playlists-menu {
		position: fixed;
		z-index: 1001;
		min-width: 190px;
		max-width: calc(100vw - 16px);
		max-height: calc(100vh - 16px);
		overflow: auto;
		padding: 4px;
		border: 1px solid var(--rb-border);
		border-radius: 3px;
		background: var(--rb-panel-raised);
		box-shadow: 0 5px 18px rgb(0 0 0 / 45%);
	}
	.track-playlists-menu button {
		display: block;
		width: 100%;
		padding: 5px 8px;
		border: 0;
		border-radius: 2px;
		background: transparent;
		color: var(--rb-text);
		font: inherit;
		text-align: left;
	}
	.track-playlists-menu button:not(:disabled):hover,
	.track-playlists-menu button:not(:disabled):focus-visible {
		background: var(--rb-select);
	}
	.track-playlists-menu button:disabled {
		color: var(--rb-text-dim);
		cursor: not-allowed;
	}
</style>
