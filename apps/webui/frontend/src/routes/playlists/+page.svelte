<script lang="ts">
	/**
	 * ARTTHUMB-8728d: every playlist as a card (232 px art, name, track count),
	 * Spotify's "Public Playlists" grid. A lazy route, charged to its own
	 * playlists-grid bundle bucket. Agent parity: GET /api/v1/playlists plus
	 * GET /api/v1/playlists/{id}/thumbnail?size=232 (opendj playlist thumbnail).
	 */
	import { onMount } from 'svelte';
	import { listPlaylists, type PlaylistSummary } from '$lib/api';
	import { artThumbnailsAvailable, playlistThumbnailUrl } from '$lib/rb/library-sidebar-art.svelte';

	let playlists = $state<PlaylistSummary[] | null>(null);
	let loadError = $state<string | null>(null);

	onMount(() => {
		listPlaylists().then(
			(rows) => (playlists = rows),
			(err: unknown) => (loadError = err instanceof Error ? err.message : String(err))
		);
	});
</script>

<svelte:head><title>Playlists - Open DJ</title></svelte:head>

<main class="playlists-grid-page" data-testid="playlists-grid-page">
	<h1 title="Every playlist as a card with its art">Playlists</h1>
	{#if !artThumbnailsAvailable()}
		<p class="note" title="The library.art_thumbnails flag is off">Playlist art is turned off by the library.art_thumbnails flag.</p>
	{:else if loadError !== null}
		<p class="note error" role="alert" title="GET /api/v1/playlists failed">Could not load playlists: {loadError}</p>
	{:else if playlists === null}
		<p class="note" title="Waiting for GET /api/v1/playlists">Loading playlists...</p>
	{:else if playlists.length === 0}
		<p class="note" title="The library holds no playlists">No playlists yet.</p>
	{:else}
		<ul class="cards">
			{#each playlists as pl (pl.playlist_id)}
				<li>
					<a class="card" data-testid="playlist-card" href={`/playlist/${encodeURIComponent(pl.playlist_id)}`} title={`Open ${pl.name}`}>
						<img
							src={playlistThumbnailUrl(pl.playlist_id, 232)}
							width="232"
							height="232"
							loading="lazy"
							decoding="async"
							alt=""
							title={`Playlist art for ${pl.name}`}
						/>
						<b>{pl.name}</b>
						<small title="Tracks in this playlist">{pl.track_count} tracks</small>
					</a>
				</li>
			{/each}
		</ul>
	{/if}
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
	.note {
		color: var(--rb-text-dim);
	}
	.cards {
		list-style: none;
		margin: 0;
		padding: 0;
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
		gap: 18px;
	}
	.card {
		display: flex;
		flex-direction: column;
		gap: 4px;
		padding: 12px;
		border-radius: 8px;
		background: var(--rb-panel);
		color: inherit;
		text-decoration: none;
	}
	.card:hover {
		background: var(--rb-panel-raised);
	}
	.card img {
		width: 100%;
		height: auto;
		aspect-ratio: 1;
		border-radius: 6px;
		object-fit: cover;
		background: var(--rb-panel-raised);
		margin-bottom: 6px;
	}
	.card b {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.card small {
		color: var(--rb-text-dim);
	}
</style>
