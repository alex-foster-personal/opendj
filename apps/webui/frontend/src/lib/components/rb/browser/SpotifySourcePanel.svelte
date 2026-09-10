<script lang="ts">
	import type { PlaylistSummaryHydrated } from '$lib/rb/api-rb';
	import { toggleSpotifyPinned, uiPrefs } from '$lib/rb/prefs.svelte';
	import {
		normalizePlaylistQuery,
		rankSpotifyPlaylists
	} from '$lib/rb/spotify-playlist-rank';
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

	let query = $state('');
	const normalizedQuery = $derived(normalizePlaylistQuery(query));
	const pinnedIds = $derived(new Set(uiPrefs.spotify_library.pinned_ids));
	const visiblePlaylists = $derived(
		rankSpotifyPlaylists(playlists, {
			query,
			pinnedIds: uiPrefs.spotify_library.pinned_ids,
			recentIds: uiPrefs.spotify_library.recent_ids
		})
	);
	const showFilter = $derived(
		!playlistsLoading && playlistsError === null && playlists.length > 0
	);
</script>

<section class="spotify-source" aria-label="Spotify source">
	<div class="source-header">
		<button class="collection-back" onclick={oncollection}>Collection</button>
		<span class="spotify-mark">Spotify</span>
	</div>
	{#if showFilter}
		<div class="playlist-filter">
			<input
				type="text"
				bind:value={query}
				placeholder="Filter playlists"
				aria-label="Filter Spotify playlists"
				autocomplete="off"
				spellcheck="false"
				onkeydown={(e) => {
					if (e.key !== 'Escape') return;
					e.preventDefault();
					e.stopPropagation();
					query = '';
				}}
			/>
			{#if query !== ''}
				<button type="button" class="filter-clear" aria-label="Clear playlist filter" onclick={() => (query = '')}>×</button>
			{/if}
			<span class="filter-count" aria-live="polite">{visiblePlaylists.length} / {playlists.length}</span>
		</div>
	{/if}
	<div class="playlist-list" aria-label="Imported Spotify playlists">
		{#if playlistsLoading}
			<p class="source-state">Loading imported Spotify playlists...</p>
		{:else if playlistsError !== null}
			<p class="source-state source-error">Spotify playlist load failed: {playlistsError}</p>
		{:else if playlists.length === 0}
			<p class="source-state">No imported Spotify playlists.</p>
		{:else if visiblePlaylists.length === 0}
			<p class="source-state">No playlists match "{normalizedQuery}"</p>
		{:else}
			{#each visiblePlaylists as playlist (playlist.playlist_id)}
				{@const pinned = pinnedIds.has(playlist.playlist_id)}
				<div class="playlist-row-wrap" class:selected={selectedId === playlist.playlist_id}>
					<button
						type="button"
						class="pin-toggle"
						class:pinned
						aria-label={pinned ? `Unpin ${playlist.name}` : `Pin ${playlist.name}`}
						aria-pressed={pinned}
						onclick={(e) => {
							e.stopPropagation();
							toggleSpotifyPinned(playlist.playlist_id);
						}}
					>
						<svg viewBox="0 0 16 16" width="10" height="10" aria-hidden="true">
							{#if pinned}
								<path
									fill="currentColor"
									d="M8 1.6c1.7 0 3.1 1.3 3.1 2.9 0 2.4-3.1 7.3-3.1 7.3S4.9 6.9 4.9 4.5C4.9 2.9 6.3 1.6 8 1.6zm0 1.7a1.3 1.3 0 1 0 0 2.6 1.3 1.3 0 0 0 0-2.6z"
								/>
							{:else}
								<path
									fill="none"
									stroke="currentColor"
									stroke-width="1.25"
									d="M8 1.6c1.7 0 3.1 1.3 3.1 2.9 0 2.4-3.1 7.3-3.1 7.3S4.9 6.9 4.9 4.5C4.9 2.9 6.3 1.6 8 1.6z"
								/>
							{/if}
						</svg>
					</button>
					<button
						class="playlist-row"
						type="button"
						onclick={() => onselect(playlist)}
					>
						<span class="playlist-name" title={playlist.name}>{playlist.name}</span>
						<span class="playlist-count">{playlist.track_count}</span>
					</button>
				</div>
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
	.playlist-filter { display: flex; align-items: center; gap: 4px; height: 22px; padding: 0 6px; border-bottom: 1px solid var(--rb-border); }
	.playlist-filter input { flex: 1; min-width: 0; height: 18px; padding: 0 4px; border: 1px solid var(--rb-border); background: transparent; color: var(--rb-text); font: inherit; font-size: var(--rb-fs-label); }
	.playlist-filter input::placeholder { color: var(--rb-text-dim); }
	.filter-clear { flex: none; width: 14px; height: 14px; padding: 0; border: 0; border-radius: 50%; background: transparent; color: var(--rb-text-dim); font-size: 12px; line-height: 1; cursor: pointer; }
	.filter-clear:hover { color: var(--rb-text); background: rgba(255, 255, 255, 0.08); }
	.filter-count { flex: none; color: var(--rb-text-dim); font-size: var(--rb-fs-label); font-variant-numeric: tabular-nums; }
	.playlist-list { flex: none; max-height: 38%; overflow-y: auto; padding: 2px 0; border-bottom: 1px solid var(--rb-border); }
	.playlist-row-wrap { display: flex; align-items: center; width: 100%; height: 22px; }
	.playlist-row-wrap:hover { background: var(--rb-panel-raised); }
	.playlist-row-wrap.selected { background: var(--rb-select); }
	.pin-toggle { flex: none; display: inline-flex; align-items: center; justify-content: center; width: 18px; height: 18px; margin-left: 2px; padding: 0; border: 0; background: transparent; color: var(--rb-text-dim); cursor: pointer; }
	.pin-toggle.pinned { color: var(--rb-accent); }
	.playlist-row { display: flex; align-items: center; gap: 5px; flex: 1; min-width: 0; height: 22px; padding: 0 6px 0 2px; border: 0; background: transparent; color: var(--rb-text); font: inherit; text-align: left; cursor: pointer; }
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
