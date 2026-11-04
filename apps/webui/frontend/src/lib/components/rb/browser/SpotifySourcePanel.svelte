<script lang="ts">
	import { untrack } from 'svelte';
	import type { PlaylistSummaryHydrated } from '$lib/rb/api-rb';
	import { setSpotifyShrinkMain, toggleSpotifyPinned, uiPrefs } from '$lib/rb/prefs.svelte';
	import { SPOTIFY_PURCHASE_SOURCES, type SpotifyPendingTrack } from '$lib/rb/spotify-api';
	import {
		getSpotifyAccountPlaylists,
		getSpotifyConnectStatus,
		saveSpotifyClientId,
		spotifyConnectRunsOnThisPage,
		startSpotifyConnect,
		type SpotifyAccountListing
	} from '$lib/rb/spotify-account-api';
	import type { SpotifyPanelRow } from '$lib/rb/spotify-account-merge';
	import { nativeShellKind, openExternal } from '$lib/shell/native-shell';
	import { hasSidePlayer, importBrowserLogins, openSpotifySidePlayer } from '$lib/shell/side-player-shell';
	import SpotifySourcePanelView from './SpotifySourcePanelView.svelte';

	let {
		playlists,
		playlistsLoading,
		playlistsError,
		selectedId,
		pendingTracks,
		loading,
		error,
		oncollection,
		onselect,
		initialAccount = null
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
		/** An account listing already in hand: painted at once, then refreshed from the engine. */
		initialAccount?: SpotifyAccountListing | null;
	} = $props();

	// SPOT-6555: every playlist on the signed-in account, not only imported ones.
	let account = $state<SpotifyAccountListing | null>(untrack(() => initialAccount));
	let accountLoading = $state(untrack(() => initialAccount === null));
	let accountError = $state<string | null>(null);
	let connectNote = $state<string | null>(null);
	let playerNote = $state<string | null>(null);
	const sidePlayerDocked = hasSidePlayer();

	async function loadAccount(): Promise<void> {
		accountLoading = true;
		accountError = null;
		try {
			account = await getSpotifyAccountPlaylists();
		} catch (exc) {
			accountError = String(exc instanceof Error ? exc.message : exc);
		} finally {
			accountLoading = false;
		}
	}

	$effect(() => {
		void loadAccount();
	});
	// The sign-in redirect lands on the engine's loopback port, so Connect
	// only works with the browser on the engine's machine.
	const connectHere = spotifyConnectRunsOnThisPage(nativeShellKind() !== null);

	async function connect(): Promise<void> {
		connectNote = 'Opening Spotify sign-in...';
		// In a plain browser tab, open the window inside the click: once the
		// request below is awaited the click's user activation can lapse and
		// the browser blocks the pop-up. It is pointed at Spotify afterwards.
		const native = nativeShellKind() !== null;
		const popup = native ? null : window.open('about:blank', '_blank');
		if (!native && popup === null) {
			connectNote = 'Your browser blocked the Spotify sign-in window; allow pop-ups for Open DJ and try again.';
			return;
		}
		if (popup !== null) popup.opener = null;
		let pointed = false;
		try {
			const url = await startSpotifyConnect();
			if (popup !== null) {
				popup.location.href = url;
				pointed = true;
			} else await openExternal(url);
			connectNote = 'Finish signing in in your browser.';
			for (;;) {
				await new Promise((resolve) => setTimeout(resolve, 1000));
				const status = await getSpotifyConnectStatus();
				if (status.phase === 'waiting') continue;
				connectNote = status.phase === 'connected' ? null : `Sign-in failed: ${status.detail ?? 'unknown'}`;
				break;
			}
			await loadAccount();
		} catch (exc) {
			if (popup !== null && !pointed) popup.close();
			connectNote = `Sign-in failed: ${String(exc instanceof Error ? exc.message : exc)}`;
		}
	}

	async function play(row: Pick<SpotifyPanelRow<PlaylistSummaryHydrated>, 'open_url'>): Promise<void> {
		if (row.open_url === null) return;
		playerNote = null;
		try {
			await openSpotifySidePlayer(row.open_url, {
				shrinkMain: uiPrefs.spotify_library.shrink_main_for_player
			});
		} catch (exc) {
			playerNote = `Could not open the player: ${String(exc instanceof Error ? exc.message : exc)}`;
		}
	}

	async function importLogins(): Promise<void> {
		playerNote = 'Importing sign-ins from Chrome...';
		try {
			const outcome = await importBrowserLogins();
			playerNote =
				outcome.kind === 'imported'
					? `Imported ${outcome.cookies} Chrome cookies for ${outcome.sites.join(', ') || 'no listed sites'}.`
					: outcome.reason;
		} catch (exc) {
			// A Cookies file that cannot be copied or read, or a schema Chrome
			// changed: say so instead of leaving "Importing..." up forever.
			playerNote = `Could not import Chrome sign-ins: ${exc instanceof Error ? exc.message : String(exc)}`;
		}
	}

	// Saving the Client ID is a plain PUT to the engine, so it works from a
	// remote browser too; only the sign-in after it must run on the engine's machine.
	let clientIdSaving = $state(false);
	async function saveClientId(clientId: string): Promise<boolean> {
		clientIdSaving = true;
		connectNote = null;
		try {
			await saveSpotifyClientId(clientId);
			await loadAccount();
			return true;
		} catch (exc) {
			connectNote = `Could not save the Client ID: ${String(exc instanceof Error ? exc.message : exc)}`;
			return false;
		} finally {
			clientIdSaving = false;
		}
	}
</script>

<SpotifySourcePanelView
	{playlists}
	{playlistsLoading}
	{playlistsError}
	{selectedId}
	{pendingTracks}
	{loading}
	{error}
	pinnedIds={uiPrefs.spotify_library.pinned_ids}
	recentIds={uiPrefs.spotify_library.recent_ids}
	purchaseSources={SPOTIFY_PURCHASE_SOURCES}
	{account}
	{accountLoading}
	{accountError}
	{connectNote}
	{playerNote}
	{connectHere}
	nativeShell={nativeShellKind() !== null}
	{sidePlayerDocked}
	shrinkMain={uiPrefs.spotify_library.shrink_main_for_player}
	{clientIdSaving}
	{oncollection}
	{onselect}
	ontogglepin={toggleSpotifyPinned}
	onrefresh={() => void loadAccount()}
	onconnect={() => void connect()}
	onplay={(row) => void play(row)}
	onimportlogins={() => void importLogins()}
	onshrinkchange={setSpotifyShrinkMain}
	onsaveclientid={saveClientId}
	onopenexternal={(url) => void openExternal(url)}
/>
