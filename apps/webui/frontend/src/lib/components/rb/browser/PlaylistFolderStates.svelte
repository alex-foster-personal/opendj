<script lang="ts">
	let {
		nodeCount,
		playlistsLoading,
		playlistsError,
		hiddenBrokenPlaylistCount,
		oncreate
	}: {
		nodeCount: number;
		playlistsLoading: boolean;
		playlistsError: string | null;
		hiddenBrokenPlaylistCount: number;
		/** undefined = creation isn't wired (same absent-means-inert convention
		 * as PlaylistTree's own oncreateplaylist gate); renders plain inert
		 * text instead of an unusable button. Not `?:` - the caller passes
		 * `undefined` explicitly (ternary on oncreateplaylist), which
		 * exactOptionalPropertyTypes rejects for an optional-shaped prop. */
		oncreate: (() => void) | undefined;
	} = $props();
</script>

{#if nodeCount === 0 && playlistsLoading}
	<div class="row child rb-inert" data-testid="playlists-loading">Loading playlists...</div>
{:else if nodeCount === 0 && playlistsError !== null}
	<div class="row child rb-inert" data-testid="playlists-error" title={playlistsError}>Playlist load failed</div>
{:else if nodeCount === 0 && hiddenBrokenPlaylistCount === 0}
	{#if oncreate}
		<button type="button" class="row child empty-cta" data-testid="playlists-empty-cta" onclick={oncreate}>
			+ Create your first playlist
		</button>
	{:else}
		<div class="row child rb-inert" data-testid="playlists-empty">no playlists yet</div>
	{/if}
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
	.empty-cta {
		width: 100%;
		border: none;
		background: transparent;
		color: var(--rb-accent);
		font: inherit;
		text-align: left;
		cursor: pointer;
	}
	.empty-cta:hover {
		background: var(--rb-panel-raised);
	}
</style>
