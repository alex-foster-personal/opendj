<script lang="ts" module>
	export type PlaylistsMenuAnchor = { x: number; y: number; stableId: string };
	export type RelocateMenuAnchor = { x: number; y: number; stableId: string; title: string | null };
</script>

<script lang="ts">
	/**
	 * Lazy mount point for the two popovers a track row's context menu opens
	 * (issue #3886): "Show in playlists" (TrackPlaylistsPopover) and
	 * "Relocate" (RelocatePopover). Both exist only after a right-click and a
	 * menu pick, yet a static import in TrackTable shipped them, their API
	 * clients and their styles in every /performance page load. Each is now
	 * fetched by the pick that opens it, the pattern MidiPanel uses for its
	 * device list.
	 *
	 * Requirements (mini-PRD):
	 *   ✔︎ Neither popover module is imported until its anchor is set.
	 *     [if] TrackTable's chunk still statically imports either popover [then] ⛔️
	 *   ✔︎ A failed fetch is shown inline at the click point with the error
	 *     text, and dismissing it clears the anchor like the popover would.
	 *     [if] the import rejects and the pick shows nothing [then] ⛔️
	 *   ✔︎ A failed fetch is retried by the next pick, not on every render.
	 *     [if] a second pick after a failure never retries [then] ⛔️
	 */
	import type { Component } from 'svelte';

	type PlaylistsProps = { stableId: string; x: number; y: number; onclose: () => void };
	type RelocateProps = PlaylistsProps & { trackTitle: string | null; onrelocated: () => void };

	let {
		playlistsMenu = $bindable(null),
		relocateMenu = $bindable(null),
		onrelocated = undefined
	}: {
		playlistsMenu?: PlaylistsMenuAnchor | null;
		relocateMenu?: RelocateMenuAnchor | null;
		onrelocated?: (() => void) | undefined;
	} = $props();

	let PlaylistsPopover: Component<PlaylistsProps> | null = $state(null);
	let RelocatePopover: Component<RelocateProps> | null = $state(null);
	let playlistsError: string | null = $state(null);
	let relocateError: string | null = $state(null);
	let playlistsLoading = false;
	let relocateLoading = false;

	function _message(exc: unknown): string {
		return exc instanceof Error ? exc.message : String(exc);
	}

	$effect(() => {
		if (playlistsMenu === null || PlaylistsPopover !== null || playlistsLoading) return;
		playlistsLoading = true;
		playlistsError = null;
		import('./TrackPlaylistsPopover.svelte')
			.then((m) => {
				PlaylistsPopover = m.default;
			})
			.catch((exc: unknown) => {
				console.error('[track-table] Show in playlists popover failed to load', exc);
				playlistsError = _message(exc);
			})
			.finally(() => {
				playlistsLoading = false;
			});
	});

	$effect(() => {
		if (relocateMenu === null || RelocatePopover !== null || relocateLoading) return;
		relocateLoading = true;
		relocateError = null;
		import('./RelocatePopover.svelte')
			.then((m) => {
				RelocatePopover = m.default;
			})
			.catch((exc: unknown) => {
				console.error('[track-table] Relocate popover failed to load', exc);
				relocateError = _message(exc);
			})
			.finally(() => {
				relocateLoading = false;
			});
	});
</script>

{#if playlistsMenu !== null && PlaylistsPopover !== null}
	<PlaylistsPopover
		stableId={playlistsMenu.stableId}
		x={playlistsMenu.x}
		y={playlistsMenu.y}
		onclose={() => (playlistsMenu = null)}
	/>
{:else if playlistsMenu !== null && playlistsError !== null}
	<div class="popover-load-error" role="alert" style={`left:${playlistsMenu.x}px;top:${playlistsMenu.y}px`}>
		<span>Show in playlists failed to load: {playlistsError}</span>
		<button type="button" onclick={() => (playlistsMenu = null)}>Close</button>
	</div>
{/if}
{#if relocateMenu !== null && RelocatePopover !== null}
	<RelocatePopover
		stableId={relocateMenu.stableId}
		trackTitle={relocateMenu.title}
		x={relocateMenu.x}
		y={relocateMenu.y}
		onclose={() => (relocateMenu = null)}
		onrelocated={() => onrelocated?.()}
	/>
{:else if relocateMenu !== null && relocateError !== null}
	<div class="popover-load-error" role="alert" style={`left:${relocateMenu.x}px;top:${relocateMenu.y}px`}>
		<span>Relocate failed to load: {relocateError}</span>
		<button type="button" onclick={() => (relocateMenu = null)}>Close</button>
	</div>
{/if}

<style>
	.popover-load-error {
		position: fixed;
		z-index: 1001;
		display: flex;
		gap: 8px;
		align-items: center;
		max-width: 360px;
		padding: 6px 10px;
		background: var(--rb-panel);
		border: 1px solid var(--rb-red);
		border-radius: 4px;
		color: var(--rb-red);
		font-size: 11px;
	}
	.popover-load-error button {
		background: transparent;
		border: 1px solid var(--rb-border);
		color: var(--rb-text);
		font-size: 11px;
		cursor: pointer;
	}
</style>
