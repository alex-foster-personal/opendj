<script lang="ts">
	// SpotifySourcePanel as props only: the container talks to the engine,
	// the desktop shell and prefs; this view owns only its own UI state (the
	// filter query and the Client ID draft) and merges and ranks what it is given.
	import type { components } from '$lib/api-types';
	import { normalizePlaylistQuery, rankSpotifyPlaylists } from '$lib/rb/spotify-playlist-rank';
	import type { SpotifyPendingTrack, SpotifyPurchaseSource } from '$lib/rb/spotify-api';
	import type { SpotifyAccountListing } from '$lib/rb/spotify-account-api';
	import { isSpotifyClientId, spotifyNotConnectedText } from '$lib/rb/spotify-account-copy';
	import {
		importedAccountCount,
		mergeSpotifyPlaylists,
		type SpotifyPanelRow
	} from '$lib/rb/spotify-account-merge';

	// PlaylistSummaryHydrated's shape, spelled from the schema so this view
	// adds no importer to api-rb.ts (the quality gate's max_fan_in module).
	type SpotifyPlaylist = components['schemas']['PlaylistSummary'] & { available_count: number };

	let {
		playlists,
		playlistsLoading,
		playlistsError,
		selectedId,
		pendingTracks,
		loading,
		error,
		pinnedIds,
		recentIds,
		purchaseSources,
		account,
		accountLoading,
		accountError,
		connectNote,
		playerNote,
		connectHere,
		nativeShell,
		sidePlayerDocked,
		shrinkMain,
		clientIdSaving,
		oncollection,
		onselect,
		ontogglepin,
		onrefresh,
		onconnect,
		onplay,
		onimportlogins,
		onshrinkchange,
		onsaveclientid,
		onopenexternal
	}: {
		playlists: SpotifyPlaylist[];
		playlistsLoading: boolean;
		playlistsError: string | null;
		selectedId: string | null;
		pendingTracks: SpotifyPendingTrack[] | null;
		loading: boolean;
		error: string | null;
		pinnedIds: readonly string[];
		recentIds: readonly string[];
		purchaseSources: readonly SpotifyPurchaseSource[];
		/** SPOT-6555: the signed-in account's listing, or null before the first answer. */
		account: SpotifyAccountListing | null;
		accountLoading: boolean;
		accountError: string | null;
		connectNote: string | null;
		playerNote: string | null;
		/** The sign-in redirect lands on the engine's loopback port, so Connect only works on its machine. */
		connectHere: boolean;
		/** Inside a desktop shell, which opens outside links in the system browser. */
		nativeShell: boolean;
		sidePlayerDocked: boolean;
		shrinkMain: boolean;
		clientIdSaving: boolean;
		oncollection: () => void;
		onselect: (playlist: SpotifyPlaylist) => void;
		ontogglepin: (playlistId: string) => void;
		onrefresh: () => void;
		onconnect: () => void;
		onplay: (row: SpotifyPanelRow<SpotifyPlaylist>) => void;
		onimportlogins: () => void;
		onshrinkchange: (shrink: boolean) => void;
		/** Resolves true once the Client ID is saved, so the draft can clear. */
		onsaveclientid: (clientId: string) => Promise<boolean>;
		onopenexternal: (url: string) => void;
	} = $props();

	// A fresh install has no Spotify app yet: the user pastes their own app's
	// public Client ID once, then Connect appears.
	const SPOTIFY_DASHBOARD_URL = 'https://developer.spotify.com/dashboard';
	let clientIdDraft = $state('');
	async function submitClientId(): Promise<void> {
		if (await onsaveclientid(clientIdDraft)) clientIdDraft = '';
	}

	let query = $state('');
	const normalizedQuery = $derived(normalizePlaylistQuery(query));
	const pinnedSet = $derived(new Set(pinnedIds));
	const connected = $derived(account !== null && account.connected);
	// While the imported list loads, the account's own rows are listed alone: imports join them when they land.
	const rows = $derived(
		mergeSpotifyPlaylists(
			account !== null && account.connected ? account.playlists : null,
			playlistsLoading ? [] : playlists
		)
	);
	const visibleRows = $derived(rankSpotifyPlaylists(rows, { query, pinnedIds, recentIds }));
	// A failed imported-list load must not hide the account's own playlists: the error is reported above the rows, not instead of them.
	const showFilter = $derived(rows.length > 0);
	const importedCount = $derived(importedAccountCount(rows));
</script>

{#snippet playlistRow(row: SpotifyPanelRow<SpotifyPlaylist>)}
	{@const pinned = pinnedSet.has(row.playlist_id)}
	{@const imported = row.imported}
	<div class="playlist-row-wrap" class:selected={selectedId === row.playlist_id}>
		<button
			type="button"
			class="pin-toggle"
			class:pinned
			aria-label={pinned ? `Unpin ${row.name}` : `Pin ${row.name}`}
			aria-pressed={pinned}
			onclick={(e) => {
				e.stopPropagation();
				ontogglepin(row.playlist_id);
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
			class:not-imported={imported === null}
			type="button"
			title={imported !== null
				? `${row.name}: imported into Open DJ. Click for its Buy list.`
				: `${row.name}: not imported. Click to play it in the Spotify side player.`}
			onclick={() => (imported !== null ? onselect(imported) : onplay(row))}
		>
			<span class="playlist-name">{row.name}</span>
			{#if imported !== null}<span class="imported-mark" aria-label="Imported into Open DJ">●</span
				>{/if}
			<span class="playlist-count" title="Tracks in this playlist">{row.track_count ?? '?'}</span>
		</button>
		{#if row.open_url !== null}
			<button
				type="button"
				class="play-btn"
				aria-label={`Play ${row.name} in the Spotify side player`}
				title={sidePlayerDocked
					? 'Play in the Spotify side player, docked beside Open DJ'
					: 'Play in a Spotify pop-up (the desktop app docks it beside Open DJ)'}
				onclick={(e) => {
					e.stopPropagation();
					onplay(row);
				}}
			>
				<svg viewBox="0 0 10 10" width="8" height="8" aria-hidden="true"
					><path fill="currentColor" d="M2 1l7 4-7 4z" /></svg
				>
			</button>
		{:else}
			<!-- A row with no Spotify id keeps its play button, inert with the reason. -->
			<button
				type="button"
				class="play-btn"
				disabled
				aria-label={`Play ${row.name} in the Spotify side player (unavailable)`}
				title="No Spotify playlist id was saved for this imported playlist, so the player cannot open it; import it again from Spotify"
			>
				<svg viewBox="0 0 10 10" width="8" height="8" aria-hidden="true"
					><path fill="currentColor" d="M2 1l7 4-7 4z" /></svg
				>
			</button>
		{/if}
	</div>
{/snippet}

<section class="spotify-source" aria-label="Spotify source">
	<div class="source-header">
		<button class="collection-back" onclick={oncollection}>Collection</button>
		<span class="spotify-mark">Spotify</span>
	</div>
	<div class="account-line" aria-live="polite">
		{#if accountLoading}
			<span class="source-state-inline">Checking your Spotify account...</span>
		{:else if accountError !== null}
			<span class="source-state-inline source-error"
				>Spotify account load failed: {accountError}</span
			>
			<button type="button" class="link-btn" onclick={onrefresh}>Retry</button>
		{:else if account !== null && account.connected}
			<span
				class="source-state-inline"
				title="Every playlist your Spotify account owns or follows, read live from Spotify. {importedCount} of them are imported into Open DJ."
				>{account.display_name ?? account.user_id} · {account.playlists.length} playlists</span
			>
			<button
				type="button"
				class="link-btn"
				title="Read the list from Spotify again"
				onclick={onrefresh}>Refresh</button
			>
		{:else if account !== null && !account.connected}
			<span class="source-state-inline">{spotifyNotConnectedText(account.reason)}</span>
			{#if account.reason === 'no_client_id'}
				<form
					class="client-id-form"
					onsubmit={(e) => {
						e.preventDefault();
						void submitClientId();
					}}
				>
					<input
						type="text"
						class="client-id-input"
						aria-label="Spotify Client ID"
						placeholder="Spotify Client ID"
						spellcheck="false"
						autocomplete="off"
						bind:value={clientIdDraft}
					/>
					<button
						type="submit"
						class="connect-btn"
						aria-label="Save Spotify Client ID"
						disabled={clientIdSaving || !isSpotifyClientId(clientIdDraft)}
						title={isSpotifyClientId(clientIdDraft)
							? 'Save this Client ID on this machine, then sign in with Connect Spotify.'
							: 'Paste the 32-character Client ID from your Spotify app first.'}>Save</button
					>
					<a
						class="link-btn"
						href={SPOTIFY_DASHBOARD_URL}
						target="_blank"
						rel="noopener noreferrer"
						title="Create a free app at developer.spotify.com/dashboard, add the redirect URI http://127.0.0.1:8888/callback, and paste its Client ID here. It is a public id, not a password."
						onclick={(e) => {
							// The desktop shell opens it in the system browser, not a new app window.
							if (!nativeShell) return;
							e.preventDefault();
							onopenexternal(SPOTIFY_DASHBOARD_URL);
						}}>Get one</a
					>
				</form>
			{/if}
			{#if account.reason === 'not_signed_in' || account.reason === 'scope_missing' || account.reason === 'token_refresh_failed'}
				{#if connectHere}
					<button type="button" class="connect-btn" onclick={onconnect}>Connect Spotify</button>
				{:else}
					<span class="source-state-inline"
						>Open DJ is running on another machine: sign in there (open Open DJ on that machine, or
						run
						<code>python -m apps.spotify login</code> there).</span
					>
				{/if}
			{/if}
		{/if}
		{#if connectNote !== null}<span class="source-state-inline">{connectNote}</span>{/if}
	</div>
	<div class="player-bar">
		<button
			type="button"
			class="link-btn"
			disabled={!sidePlayerDocked}
			title={sidePlayerDocked
				? 'Copy your Chrome sign-ins for Spotify, SoundCloud, Beatport and other music sites into the side player, so you do not have to find your passwords. Asks first; nothing leaves this Mac.'
				: 'Desktop app only: import your Chrome sign-ins into the side player.'}
			onclick={onimportlogins}>Import logins from Chrome</button
		>
		<label
			class="shrink-toggle"
			title="When the Spotify side player opens, Open DJ gets narrower to make room instead of being covered."
		>
			<input
				type="checkbox"
				checked={shrinkMain}
				onchange={(e) => onshrinkchange(e.currentTarget.checked)}
			/>
			Shrink Open DJ for player
		</label>
	</div>
	{#if playerNote !== null}<p class="source-state">{playerNote}</p>{/if}
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
				<button
					type="button"
					class="filter-clear"
					aria-label="Clear playlist filter"
					onclick={() => (query = '')}>×</button
				>
			{/if}
			<span
				class="filter-count"
				aria-live="polite"
				title="Playlists matching the filter / all listed playlists (your account plus imported ones)"
				>{visibleRows.length} / {rows.length}</span
			>
		</div>
	{/if}
	<div class="playlist-list" aria-label="Spotify playlists">
		{#if playlistsError !== null}
			<p class="source-state source-error">Spotify playlist load failed: {playlistsError}</p>
		{/if}
		{#if playlistsLoading}
			<p class="source-state">Loading imported Spotify playlists...</p>
			<!-- The account's rows do not wait on the imported list (state.db), however slow it is. -->
			{#each visibleRows as row (row.playlist_id)}{@render playlistRow(row)}{/each}
		{:else if rows.length === 0 && playlistsError === null}
			<p class="source-state">
				{connected ? 'No playlists on this Spotify account.' : 'No imported Spotify playlists.'}
			</p>
		{:else if rows.length > 0 && visibleRows.length === 0}
			<p class="source-state">No playlists match "{normalizedQuery}"</p>
		{:else if rows.length > 0}
			{#each visibleRows as row (row.playlist_id)}{@render playlistRow(row)}{/each}
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
							{#each purchaseSources as source (source)}
								<a href={track.suggested_sources[source]} target="_blank" rel="noreferrer"
									>{source}</a
								>
							{/each}
						</div>
					</article>
				{/each}
			</div>
		{/if}
	</div>
</section>

<style>
	.spotify-source {
		display: flex;
		flex-direction: column;
		min-height: 0;
		height: 100%;
		background: var(--rb-panel);
	}
	.source-header,
	.buy-list-heading {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 6px;
		min-height: 24px;
		padding: 0 6px;
		border-bottom: 1px solid var(--rb-border);
		color: var(--rb-text);
		font-size: var(--rb-fs-label);
	}
	.collection-back {
		border: 0;
		padding: 0;
		background: transparent;
		color: var(--rb-accent);
		font: inherit;
		cursor: pointer;
	}
	.spotify-mark {
		color: #35c04f;
		font-weight: 600;
	}
	.playlist-filter {
		display: flex;
		align-items: center;
		gap: 4px;
		height: 22px;
		padding: 0 6px;
		border-bottom: 1px solid var(--rb-border);
	}
	.playlist-filter input {
		flex: 1;
		min-width: 0;
		height: 18px;
		padding: 0 4px;
		border: 1px solid var(--rb-border);
		background: transparent;
		color: var(--rb-text);
		font: inherit;
		font-size: var(--rb-fs-label);
	}
	.playlist-filter input::placeholder {
		color: var(--rb-text-dim);
	}
	.filter-clear {
		flex: none;
		width: 14px;
		height: 14px;
		padding: 0;
		border: 0;
		border-radius: 50%;
		background: transparent;
		color: var(--rb-text-dim);
		font-size: 12px;
		line-height: 1;
		cursor: pointer;
	}
	.filter-clear:hover {
		color: var(--rb-text);
		background: rgba(255, 255, 255, 0.08);
	}
	.filter-count {
		flex: none;
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-label);
		font-variant-numeric: tabular-nums;
	}
	.playlist-list {
		flex: none;
		max-height: 38%;
		overflow-y: auto;
		padding: 2px 0;
		border-bottom: 1px solid var(--rb-border);
	}
	.playlist-row-wrap {
		display: flex;
		align-items: center;
		width: 100%;
		height: 22px;
	}
	.playlist-row-wrap:hover {
		background: var(--rb-panel-raised);
	}
	.playlist-row-wrap.selected {
		background: var(--rb-select);
	}
	.pin-toggle {
		flex: none;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 18px;
		height: 18px;
		margin-left: 2px;
		padding: 0;
		border: 0;
		background: transparent;
		color: var(--rb-text-dim);
		cursor: pointer;
	}
	.pin-toggle.pinned {
		color: var(--rb-accent);
	}
	.playlist-row {
		display: flex;
		align-items: center;
		gap: 5px;
		flex: 1;
		min-width: 0;
		height: 22px;
		padding: 0 6px 0 2px;
		border: 0;
		background: transparent;
		color: var(--rb-text);
		font: inherit;
		text-align: left;
		cursor: pointer;
	}
	.playlist-name {
		flex: 1;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.playlist-count {
		color: var(--rb-text-dim);
		font-variant-numeric: tabular-nums;
	}
	.playlist-row.not-imported .playlist-name {
		color: var(--rb-text-dim);
	}
	.imported-mark {
		flex: none;
		color: var(--rb-accent);
		font-size: 7px;
	}
	.play-btn {
		flex: none;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 18px;
		height: 18px;
		margin-right: 2px;
		padding: 0;
		border: 0;
		background: transparent;
		color: var(--rb-text-dim);
		cursor: pointer;
	}
	.play-btn:hover {
		color: var(--rb-text);
	}
	.play-btn:disabled {
		opacity: 0.4;
		cursor: not-allowed;
	}
	/* flex: none: in the panel's column a wrapped status line must push the bar
	   below it down, not shrink under it (the bar then covered Connect). */
	.account-line,
	.player-bar {
		flex: none;
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 6px;
		min-height: 22px;
		padding: 2px 6px;
		border-bottom: 1px solid var(--rb-border);
		font-size: var(--rb-fs-label);
	}
	.source-state-inline {
		color: var(--rb-text-dim);
	}
	.link-btn {
		border: 0;
		padding: 0;
		background: transparent;
		color: var(--rb-accent);
		font: inherit;
		cursor: pointer;
	}
	.link-btn:disabled {
		color: var(--rb-text-dim);
		cursor: default;
	}
	.connect-btn {
		height: 18px;
		padding: 0 8px;
		border: 1px solid var(--rb-border);
		border-radius: 9px;
		background: transparent;
		color: var(--rb-text);
		font: inherit;
		cursor: pointer;
	}
	.connect-btn:hover {
		background: var(--rb-panel-raised);
	}
	.connect-btn:disabled {
		color: var(--rb-text-dim);
		cursor: default;
	}
	.client-id-form {
		display: inline-flex;
		align-items: center;
		gap: 6px;
	}
	.client-id-input {
		width: 22ch;
		height: 18px;
		padding: 0 6px;
		border: 1px solid var(--rb-border);
		border-radius: 4px;
		background: transparent;
		color: var(--rb-text);
		font: inherit;
		font-family: var(--rb-font-mono, monospace);
	}
	.shrink-toggle {
		display: inline-flex;
		align-items: center;
		gap: 3px;
		color: var(--rb-text-dim);
		cursor: pointer;
	}
	.shrink-toggle input {
		margin: 0;
	}
	.buy-list {
		display: flex;
		flex: 1;
		flex-direction: column;
		min-height: 0;
	}
	.pending-list {
		overflow-y: auto;
	}
	.pending-row {
		padding: 5px 6px;
		border-bottom: 1px solid var(--rb-border);
	}
	.pending-copy {
		display: flex;
		min-width: 0;
		flex-direction: column;
	}
	.pending-title {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		color: var(--rb-text);
	}
	.pending-artist,
	.pending-album {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-label);
	}
	.purchase-links {
		display: flex;
		flex-wrap: wrap;
		gap: 5px;
		margin-top: 3px;
		font-size: var(--rb-fs-label);
	}
	.purchase-links a {
		color: var(--rb-accent);
		text-decoration: none;
	}
	.purchase-links a:hover {
		text-decoration: underline;
	}
	.source-state {
		margin: 7px 6px;
		color: var(--rb-text-dim);
		font-size: var(--rb-fs-label);
		line-height: 1.35;
	}
	.source-error {
		color: var(--rb-danger, #d0342c);
	}
</style>
