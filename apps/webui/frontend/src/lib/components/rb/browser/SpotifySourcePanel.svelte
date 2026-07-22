<script lang="ts">
	import type { PlaylistSummaryHydrated } from '$lib/rb/api-rb';
	import {
		SPOTIFY_PURCHASE_SOURCES,
		type SpotifyPendingTrack
	} from '$lib/rb/spotify-api';

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

<section class="spotify-source" aria-label="Spotify source">
	<div class="source-header">
		<button class="collection-back" onclick={oncollection}>Collection</button>
		<span class="spotify-mark">Spotify</span>
	</div>
	<div class="playlist-list" aria-label="Imported Spotify playlists">
		{#if playlistsLoading}
			<p class="source-state">Loading imported Spotify playlists...</p>
		{:else if playlistsError !== null}
			<p class="source-state source-error">Spotify playlist load failed: {playlistsError}</p>
		{:else if playlists.length === 0}
			<p class="source-state">No imported Spotify playlists.</p>
		{:else}
			{#each playlists as playlist (playlist.playlist_id)}
				<button
					class="playlist-row"
					class:selected={selectedId === playlist.playlist_id}
					onclick={() => onselect(playlist)}
				>
					<span class="playlist-name" title={playlist.name}>{playlist.name}</span>
					<span class="playlist-count">{playlist.track_count}</span>
				</button>
			{/each}
		{/if}
	</div>

	<div class="buy-list">
		<div class="buy-list-heading">
			<span>Buy list</span>
			{#if pendingTracks !== null}<span>{pendingTracks.length}</span>{/if}
		</div>
		{#if selectedId === null}
			<p class="source-state">Choose a Spotify playlist to view its persisted acquisition queue.</p>
		{:else if loading}
			<p class="source-state">Loading acquisition queue...</p>
		{:else if error !== null}
			<p class="source-state source-error">Buy-list load failed: {error}</p>
		{:else if pendingTracks !== null && pendingTracks.length === 0}
			<p class="source-state">No pending purchases for this playlist.</p>
		{:else if pendingTracks !== null}
			<div class="pending-list">
				{#each pendingTracks as track (track.pending_id)}
					<article class="pending-row">
						<div class="pending-copy">
							<span class="pending-title">{track.title}</span>
							<span class="pending-artist">{track.artist}</span>
							{#if track.album !== null}<span class="pending-album">{track.album}</span>{/if}
						</div>
						<div class="purchase-links" aria-label={`Purchase links for ${track.title}`}>
							{#each SPOTIFY_PURCHASE_SOURCES as source (source)}
								<a href={track.suggested_sources[source]} target="_blank" rel="noreferrer">{source}</a>
							{/each}
						</div>
					</article>
				{/each}
			</div>
		{/if}
	</div>
</section>

<style>
	.spotify-source { display: flex; flex-direction: column; min-height: 0; height: 100%; background: var(--rb-panel); }
	.source-header, .buy-list-heading { display: flex; align-items: center; justify-content: space-between; gap: 6px; min-height: 24px; padding: 0 6px; border-bottom: 1px solid var(--rb-border); color: var(--rb-text); font-size: var(--rb-fs-label); }
	.collection-back { border: 0; padding: 0; background: transparent; color: var(--rb-accent); font: inherit; cursor: pointer; }
	.spotify-mark { color: #35c04f; font-weight: 600; }
	.playlist-list { flex: none; max-height: 38%; overflow-y: auto; padding: 2px 0; border-bottom: 1px solid var(--rb-border); }
	.playlist-row { display: flex; align-items: center; gap: 5px; width: 100%; height: 22px; padding: 0 6px; border: 0; background: transparent; color: var(--rb-text); font: inherit; text-align: left; cursor: pointer; }
	.playlist-row:hover { background: var(--rb-panel-raised); }
	.playlist-row.selected { background: var(--rb-select); }
	.playlist-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.playlist-count { color: var(--rb-text-dim); font-variant-numeric: tabular-nums; }
	.buy-list { display: flex; flex: 1; flex-direction: column; min-height: 0; }
	.pending-list { overflow-y: auto; }
	.pending-row { padding: 5px 6px; border-bottom: 1px solid var(--rb-border); }
	.pending-copy { display: flex; min-width: 0; flex-direction: column; }
	.pending-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--rb-text); }
	.pending-artist, .pending-album { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--rb-text-dim); font-size: var(--rb-fs-label); }
	.purchase-links { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 3px; font-size: var(--rb-fs-label); }
	.purchase-links a { color: var(--rb-accent); text-decoration: none; }
	.purchase-links a:hover { text-decoration: underline; }
	.source-state { margin: 7px 6px; color: var(--rb-text-dim); font-size: var(--rb-fs-label); line-height: 1.35; }
	.source-error { color: var(--rb-danger, #d0342c); }
</style>
