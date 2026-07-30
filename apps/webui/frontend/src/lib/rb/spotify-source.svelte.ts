/**
 * Spotify browser source store - live index (disk-cached on the server) +
 * ensure-on-first-click into state.db, then TrackTable via normal playlist load.
 */

import {
	ensureSpotifyPlaylist,
	getSpotifyPendingTracks,
	listLiveSpotifyPlaylists,
	pendingToBrowseTracks,
	type SpotifyBrowseTrack,
	type SpotifyEnsureResult,
	type SpotifyLivePlaylist
} from '$lib/rb/spotify-api';
import { orderByPinsAndNew } from '$lib/rb/playlist-pin-order';
import { uiPrefs } from '$lib/rb/prefs.svelte';

const SEEN_KEY = 'mdt.rb.spotify-seen-playlist-ids.v1';

export type SpotifySourceState = {
	selectedId: string | null;
	playlists: SpotifyLivePlaylist[];
	playlistsLoading: boolean;
	playlistsError: string | null;
	browseTracks: SpotifyBrowseTrack[] | null;
	tracksLoading: boolean;
	tracksError: string | null;
	ensuring: boolean;
	lastEnsure: SpotifyEnsureResult | null;
};

function _loadSeenIds(): Set<string> {
	if (typeof window === 'undefined') return new Set();
	const raw = window.localStorage.getItem(SEEN_KEY);
	if (raw === null) return new Set();
	const parsed = JSON.parse(raw) as unknown;
	if (!Array.isArray(parsed)) {
		throw new Error(`${SEEN_KEY}: expected string array`);
	}
	return new Set(parsed.filter((x): x is string => typeof x === 'string' && x !== ''));
}

function _saveSeenIds(ids: readonly string[]): void {
	if (typeof window === 'undefined') return;
	window.localStorage.setItem(SEEN_KEY, JSON.stringify([...ids]));
}

function _orderPlaylists(lists: SpotifyLivePlaylist[]): SpotifyLivePlaylist[] {
	const seen = _loadSeenIds();
	const newIds = new Set<string>();
	// Only treat as "new" after we have a prior snapshot - first load must
	// not float all ~500 playlists to the top (new-user latency / chaos).
	if (uiPrefs.new_playlists_at_top && seen.size > 0) {
		for (const p of lists) {
			if (!seen.has(p.playlist_id)) newIds.add(p.playlist_id);
		}
	}
	const ordered = orderByPinsAndNew(
		lists,
		(p) => p.playlist_id,
		uiPrefs.pinned_playlist_ids,
		newIds
	);
	_saveSeenIds(lists.map((p) => p.playlist_id));
	return ordered;
}

export function createSpotifySource() {
	let selectedId = $state<string | null>(null);
	let playlists = $state<SpotifyLivePlaylist[]>([]);
	let playlistsLoading = $state(false);
	let playlistsError = $state<string | null>(null);
	let browseTracks = $state<SpotifyBrowseTrack[] | null>(null);
	let tracksLoading = $state(false);
	let tracksError = $state<string | null>(null);
	let ensuring = $state(false);
	let lastEnsure = $state<SpotifyEnsureResult | null>(null);
	let playlistsSeq = 0;
	let tracksSeq = 0;

	function resortPlaylists(): void {
		if (playlists.length === 0) return;
		// Re-apply pin order without inventing "new" (seen already saved).
		playlists = orderByPinsAndNew(
			playlists,
			(p) => p.playlist_id,
			uiPrefs.pinned_playlist_ids,
			new Set()
		);
	}

	async function loadPlaylists(refresh = false): Promise<void> {
		const sequence = ++playlistsSeq;
		playlistsLoading = true;
		playlistsError = null;
		try {
			const lists = await listLiveSpotifyPlaylists({ refresh });
			if (sequence !== playlistsSeq) return;
			playlists = _orderPlaylists(lists);
		} catch (exc) {
			if (sequence !== playlistsSeq) return;
			playlistsError = String(exc);
			playlists = [];
		} finally {
			if (sequence === playlistsSeq) playlistsLoading = false;
		}
	}

	async function selectPlaylist(playlist: SpotifyLivePlaylist): Promise<SpotifyEnsureResult> {
		selectedId = playlist.playlist_id;
		const sequence = ++tracksSeq;
		ensuring = true;
		tracksLoading = true;
		tracksError = null;
		browseTracks = null;
		try {
			const ensured = await ensureSpotifyPlaylist(playlist.playlist_id);
			if (sequence !== tracksSeq) return ensured;
			lastEnsure = ensured;
			try {
				const pending = await getSpotifyPendingTracks(ensured.playlist_id);
				if (sequence !== tracksSeq) return ensured;
				browseTracks = pendingToBrowseTracks(pending);
			} catch {
				if (sequence !== tracksSeq) return ensured;
				browseTracks = [];
			}
			return ensured;
		} catch (exc) {
			if (sequence !== tracksSeq) throw exc;
			tracksError = String(exc);
			throw exc;
		} finally {
			if (sequence === tracksSeq) {
				ensuring = false;
				tracksLoading = false;
			}
		}
	}

	function clearSelection(): void {
		selectedId = null;
		browseTracks = null;
		tracksError = null;
		lastEnsure = null;
	}

	return {
		get selectedId() {
			return selectedId;
		},
		set selectedId(v: string | null) {
			selectedId = v;
		},
		get playlists() {
			return playlists;
		},
		get playlistsLoading() {
			return playlistsLoading;
		},
		get playlistsError() {
			return playlistsError;
		},
		get browseTracks() {
			return browseTracks;
		},
		get tracksLoading() {
			return tracksLoading;
		},
		get tracksError() {
			return tracksError;
		},
		get ensuring() {
			return ensuring;
		},
		get lastEnsure() {
			return lastEnsure;
		},
		loadPlaylists,
		selectPlaylist,
		clearSelection,
		resortPlaylists
	};
}

export type SpotifySource = ReturnType<typeof createSpotifySource>;
