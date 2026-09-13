<script lang="ts">
	let {
		nodeCount,
		playlistsLoading,
		playlistsError,
		hiddenBrokenPlaylistCount
	}: {
		nodeCount: number;
		playlistsLoading: boolean;
		playlistsError: string | null;
		hiddenBrokenPlaylistCount: number;
	} = $props();
</script>

{#if nodeCount === 0 && playlistsLoading}
	<div class="row child rb-inert" data-testid="playlists-loading">Loading playlists...</div>
{:else if nodeCount === 0 && playlistsError !== null}
	<div class="row child rb-inert" data-testid="playlists-error" title={playlistsError}>Playlist load failed</div>
{:else if nodeCount === 0 && hiddenBrokenPlaylistCount === 0}
	<div class="row child rb-inert" data-testid="playlists-empty">no playlists yet</div>
{/if}

<style>
	.row {
		display: flex;
		align-items: center;
		gap: 5px;
		height: 20px;
		padding: 0 6px;
		color: var(--rb-text);
		white-space: nowrap;
	}
	.row.child {
		padding-left: 22px;
	}
	.row.rb-inert {
		opacity: 0.5;
		cursor: default;
	}
</style>
