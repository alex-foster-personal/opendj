<script lang="ts">
	import type { PlaylistSummary } from '$lib/api';
	import { playlistThumbnailUrl } from '$lib/rb/library-sidebar-art.svelte';

	let {
		artThumbnailsOn = true,
		playlists = null,
		loadError = null
	}: {
		/** When false, only the art-disabled notice renders (flag off). */
		artThumbnailsOn?: boolean;
		playlists: PlaylistSummary[] | null;
		loadError: string | null;
	} = $props();
</script>

{#if !artThumbnailsOn}
	<p
		class="note"
		data-testid="playlist-browser-art-off"
		title="The library.art_thumbnails flag is off"
	>
		Playlist art is turned off by the library.art_thumbnails flag.
	</p>
{:else if loadError !== null}
	<p
		class="note error"
		role="alert"
		data-testid="playlist-browser-error"
		title="GET /api/v1/playlists failed"
	>
		Could not load playlists: {loadError}
	</p>
{:else if playlists === null}
	<p class="note" data-testid="playlist-browser-loading" title="Waiting for GET /api/v1/playlists">
		Loading playlists...
	</p>
{:else if playlists.length === 0}
	<p class="note" data-testid="playlist-browser-empty" title="The library holds no playlists">
		No playlists yet.
	</p>
{:else}
	<ul class="cards" data-testid="playlist-browser-success">
		{#each playlists as pl (pl.playlist_id)}
			<li>
				<a
					class="card"
					data-testid="playlist-card"
					href={`/playlist/${encodeURIComponent(pl.playlist_id)}`}
					title={`Open ${pl.name}`}
				>
					<img
						src={playlistThumbnailUrl(pl.playlist_id, 232, pl.updated_at)}
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

<style>
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
