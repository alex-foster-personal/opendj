<script lang="ts">
	import { untrack } from 'svelte';
	import type { PlaylistSummaryHydrated } from '$lib/rb/api-rb';
	import { listPlaylistTracksPage } from '$lib/rb/api-rb';
	import { rememberSpotifyRecent, setSpotifyShrinkMain, toggleSpotifyPinned, uiPrefs } from '$lib/rb/prefs.svelte';
	import {
		SPOTIFY_PURCHASE_SOURCES,
		getSpotifyImportStatus,
		SpotifyShareReadOnlyError,
		startSpotifyImport,
		type SpotifyPendingTrack
	} from '$lib/rb/spotify-api';
	import {
		getSpotifyAccountPlaylists,
		getSpotifyConnectStatus,
		saveSpotifyClientId,
		spotifyConnectRunsOnThisPage,
		startSpotifyConnect,
		type SpotifyAccountListing
	} from '$lib/rb/spotify-account-api';
	import { playSpotifyRow, type SpotifyPanelRow } from '$lib/rb/spotify-account-merge';
	import { importedSpotifyId } from '$lib/rb/spotify-account-merge';
	import { ApiError, readApiErrorCode } from '$lib/api/rb-http';
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
		onimported,
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
		onimported?: () => void;
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

	async function play(
		row: Pick<SpotifyPanelRow<PlaylistSummaryHydrated>, 'open_url' | 'pref_key'>
	): Promise<void> {
		if (row.open_url === null) return;
		playerNote = null;
		try {
			// #9685: a played row counts as recent, but only once the player really opened.
			await playSpotifyRow(
				row,
				async (url) => {
					await openSpotifySidePlayer(url, {
						shrinkMain: uiPrefs.spotify_library.shrink_main_for_player
					});
				},
				rememberSpotifyRecent
			);
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

	let importBusy = $state(false);
	let importNote = $state<string | null>(null);
	let ownedTracks = $state<{ title: string; artist: string; album: string | null }[] | null>(
		null
	);
	let ownedLoading = $state(false);
	let ownedFetchSeq = 0;

	function resolveRowSpotifyId(row: SpotifyPanelRow<PlaylistSummaryHydrated>): string | null {
		if (row.account?.spotify_id) return row.account.spotify_id;
		if (row.imported !== null) return importedSpotifyId(row.imported);
		return null;
	}

	async function importPlaylist(row: SpotifyPanelRow<PlaylistSummaryHydrated>): Promise<void> {
		const id = resolveRowSpotifyId(row);
		if (id === null) return;
		const count = row.track_count ?? '?';
		const first = row.imported === null;
		const ok = window.confirm(
			first
				? `Import "${row.name}" (${count} tracks) into Open DJ? This snapshots the library and writes pending purchase rows.`
				: `Re-import "${row.name}" (${count} tracks)? This rewrites the Open DJ copy.`
		);
		if (!ok) return;
		importBusy = true;
		importNote = first ? 'Importing playlist...' : 'Re-importing playlist...';
		try {
			await startSpotifyImport(id, {
				confirm_playlist_id: id,
				force: !first
			});
			for (;;) {
				await new Promise((resolve) => setTimeout(resolve, 1000));
				const status = await getSpotifyImportStatus();
				if (status.phase === 'running') continue;
				if (status.phase === 'error') {
					importNote = status.error ?? 'Import failed.';
					return;
				}
				importNote = null;
				onimported?.();
				await loadAccount();
				return;
			}
		} catch (exc) {
			if (exc instanceof SpotifyShareReadOnlyError) {
				importNote = "This share is read-only; import from the engine's machine.";
				return;
			}
			if (exc instanceof ApiError && readApiErrorCode(exc) === 'SPOTIFY_REAUTH_REQUIRED') {
				importNote =
					'Spotify sign-in expired. Connect Spotify in this panel or run `python -m apps.spotify login` on the engine machine.';
				return;
			}
			importNote = `Import failed: ${String(exc instanceof Error ? exc.message : exc)}`;
		} finally {
			importBusy = false;
		}
	}

	function openDesktop(row: SpotifyPanelRow<PlaylistSummaryHydrated>): void {
		const id = resolveRowSpotifyId(row);
		if (id === null) return;
		void openExternal(`spotify:playlist:${id}`);
	}

	$effect(() => {
		const playlistId = selectedId;
		const imported = playlists.some((p) => p.playlist_id === playlistId);
		if (playlistId === null || !imported) {
			ownedTracks = null;
			ownedLoading = false;
			return;
		}
		const seq = ++ownedFetchSeq;
		ownedLoading = true;
		ownedTracks = null;
		void (async () => {
			const collected: { title: string; artist: string; album: string | null }[] = [];
			let offset = 0;
			try {
				for (;;) {
					const { page } = await listPlaylistTracksPage(playlistId, { limit: 500, offset });
					if (seq !== ownedFetchSeq) return;
					for (const track of page.tracks) {
						if (track.spotify_pending === true) continue;
						collected.push({
							title: track.title ?? '',
							artist: track.artist ?? '',
							album: null
						});
						if (collected.length >= 2000) break;
					}
					if (collected.length >= 2000 || page.next_offset === null) break;
					offset = page.next_offset;
				}
				if (seq !== ownedFetchSeq) return;
				ownedTracks = collected;
			} catch {
				if (seq !== ownedFetchSeq) return;
				ownedTracks = [];
			} finally {
				if (seq === ownedFetchSeq) ownedLoading = false;
			}
		})();
	});
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
	{importBusy}
	{importNote}
	{ownedTracks}
	{ownedLoading}
	onimport={(row) => void importPlaylist(row)}
	onopendesktop={openDesktop}
/>
