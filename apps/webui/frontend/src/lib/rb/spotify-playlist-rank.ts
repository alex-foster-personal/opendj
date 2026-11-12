/**
 * Name-filter + pin/recent/owned ranking for the Spotify source playlist
 * list (#315). Pure: no Svelte, no prefs, no fetch. Optional `owned` is
 * accepted so a later live-browse row can opt in without a panel rewrite.
 */

export type SpotifyPlaylistRankRow = {
	playlist_id: string;
	name: string;
	updated_at?: string | null;
	owned?: boolean;
	/** Pins and recents lookup key (#9685); defaults to playlist_id when absent. */
	pref_key?: string;
};

export const SPOTIFY_RECENT_CAP = 12;
export const SPOTIFY_PINNED_CAP = 50;

const _SPOTIFY_ACCOUNT_KEY = /^spotify-account:([A-Za-z0-9]+)$/;
const _SPOTIFY_IMPORTED_KEY = /^spotify:(?:user:[^:]+:)?(?:playlist:)?([A-Za-z0-9]+)$/;

/** #9685: the one key pins and recents are stored under. An account-only
 * row (`spotify-account:<sid>`) and the imported playlist (`spotify:<sid>`,
 * or the older `spotify:user:<owner>:playlist:<sid>`) share `spotify:<sid>`;
 * any other id (no Spotify id in it) is its own key. */
export function spotifyPreferenceKey(playlistId: string): string {
	const account = _SPOTIFY_ACCOUNT_KEY.exec(playlistId);
	if (account !== null) return `spotify:${account[1]}`;
	const imported = _SPOTIFY_IMPORTED_KEY.exec(playlistId);
	if (imported !== null) return `spotify:${imported[1]}`;
	return playlistId;
}

export function normalizePlaylistQuery(raw: string): string {
	return raw.trim();
}

export function playlistNameMatches(name: string, query: string): boolean {
	return name.toLocaleLowerCase().includes(query.toLocaleLowerCase());
}

function _indexMap(ids: readonly string[]): Map<string, number> {
	const map = new Map<string, number>();
	for (let i = 0; i < ids.length; i += 1) {
		if (!map.has(ids[i])) map.set(ids[i], i);
	}
	return map;
}

function _updatedAtKey(value: string | null | undefined): string | null {
	return typeof value === 'string' && value !== '' ? value : null;
}

function _compareUpdatedAt(a: string | null | undefined, b: string | null | undefined): number {
	const ka = _updatedAtKey(a);
	const kb = _updatedAtKey(b);
	if (ka === null && kb === null) return 0;
	if (ka === null) return 1;
	if (kb === null) return -1;
	if (ka === kb) return 0;
	return ka < kb ? 1 : -1;
}

function _requirePlaylistId(playlistId: string): void {
	if (typeof playlistId !== 'string' || playlistId === '') {
		throw new Error('playlist id must be a non-empty string');
	}
}

export function rankSpotifyPlaylists<T extends SpotifyPlaylistRankRow>(
	playlists: readonly T[],
	args: {
		query: string;
		pinnedIds: readonly string[];
		recentIds: readonly string[];
	}
): T[] {
	const query = normalizePlaylistQuery(args.query);
	const survivors =
		query === ''
			? playlists.slice()
			: playlists.filter((playlist) => playlistNameMatches(playlist.name, query));
	const pinIndex = _indexMap(args.pinnedIds);
	const recentIndex = _indexMap(args.recentIds);
	survivors.sort((a, b) => {
		const aKey = a.pref_key ?? a.playlist_id;
		const bKey = b.pref_key ?? b.playlist_id;
		const aPin = pinIndex.get(aKey) ?? Number.POSITIVE_INFINITY;
		const bPin = pinIndex.get(bKey) ?? Number.POSITIVE_INFINITY;
		if (aPin !== bPin) return aPin - bPin;
		const aRecent = recentIndex.get(aKey) ?? Number.POSITIVE_INFINITY;
		const bRecent = recentIndex.get(bKey) ?? Number.POSITIVE_INFINITY;
		if (aRecent !== bRecent) return aRecent - bRecent;
		const aOwned = a.owned === true;
		const bOwned = b.owned === true;
		if (aOwned !== bOwned) return aOwned ? -1 : 1;
		const byUpdated = _compareUpdatedAt(a.updated_at, b.updated_at);
		if (byUpdated !== 0) return byUpdated;
		const byName = a.name.localeCompare(b.name);
		if (byName !== 0) return byName;
		return a.playlist_id.localeCompare(b.playlist_id);
	});
	return survivors;
}

export function prependRecentId(recentIds: readonly string[], playlistId: string): string[] {
	_requirePlaylistId(playlistId);
	if (recentIds[0] === playlistId) return recentIds as string[];
	const next = [playlistId, ...recentIds.filter((id) => id !== playlistId)];
	if (next.length > SPOTIFY_RECENT_CAP) next.length = SPOTIFY_RECENT_CAP;
	return next;
}

export function togglePinnedId(pinnedIds: readonly string[], playlistId: string): string[] {
	_requirePlaylistId(playlistId);
	const idx = pinnedIds.indexOf(playlistId);
	if (idx !== -1) return pinnedIds.filter((id) => id !== playlistId);
	if (pinnedIds.length >= SPOTIFY_PINNED_CAP) return pinnedIds as string[];
	return [...pinnedIds, playlistId];
}
