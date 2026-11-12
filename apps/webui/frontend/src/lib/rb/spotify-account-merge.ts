/**
 * Merge the live account listing with the playlists imported into state.db
 * (SPOT-6555). Pure: no Svelte, no fetch.
 *
 * Before this, the Spotify panel listed only imported playlists, so an
 * account with hundreds showed a handful. Now every account playlist is a
 * row; an imported one keeps its state.db id, pins and recents use a stable
 * pref_key (`spotify:<sid>` when known), and an imported playlist the account
 * no longer follows stays listed after them.
 */
import type { SpotifyAccountPlaylist } from './spotify-account-api';
import { spotifyPreferenceKey } from './spotify-playlist-rank';

export interface ImportedSpotifyPlaylist {
	playlist_id: string;
	name: string;
	track_count: number;
	updated_at?: string | null;
	vendor_pl_id?: string | null;
}

export interface SpotifyPanelRow<I extends ImportedSpotifyPlaylist = ImportedSpotifyPlaylist> {
	/** Row identity: the state.db id when imported, else spotify-account:<sid>. */
	playlist_id: string;
	/** Pins and recents key (#9685): spotify:<sid> when the Spotify id is known, else playlist_id. Never changes on import. */
	pref_key: string;
	name: string;
	track_count: number | null;
	owned: boolean;
	updated_at: string | null;
	/** Where the side player opens it; null only for an imported row with no Spotify id. */
	open_url: string | null;
	account: SpotifyAccountPlaylist | null;
	imported: I | null;
}

export const ACCOUNT_ROW_PREFIX = 'spotify-account:';

/** The id shape side-player-shell.ts and side-player-layout.ts accept. */
const PLAYER_ID = /^[A-Za-z0-9]+$/;

/**
 * The Spotify playlist id behind an imported row: vendor_pl_id, else the id's
 * tail. The importer writes both (`spotify:<id>`, apps/spotify/state_writer.py),
 * and the older `spotify:user:<owner>:playlist:<id>` URI form is read too. A row
 * matching none has no player URL, and the panel shows its play button
 * disabled with the reason rather than dropping it.
 */
export function importedSpotifyId(playlist: ImportedSpotifyPlaylist): string | null {
	const vendor = playlist.vendor_pl_id;
	if (typeof vendor === 'string' && vendor !== '') {
		// The side player's target guard admits only base-62 ids, so a URI, a
		// URL or a damaged value gets the disabled row, not a play button whose
		// page is refused. It does not fall back to the id's
		// tail: the stored vendor id is the one the importer meant.
		return PLAYER_ID.test(vendor) ? vendor : null;
	}
	const tail = /^spotify:(?:user:[^:]+:)?(?:playlist:)?([A-Za-z0-9]+)$/.exec(playlist.playlist_id);
	return tail === null ? null : tail[1];
}

export function mergeSpotifyPlaylists<I extends ImportedSpotifyPlaylist>(
	account: readonly SpotifyAccountPlaylist[] | null,
	imported: readonly I[]
): SpotifyPanelRow<I>[] {
	const importedBySpotifyId = new Map<string, I>();
	for (const playlist of imported) {
		const sid = importedSpotifyId(playlist);
		if (sid !== null && !importedBySpotifyId.has(sid)) importedBySpotifyId.set(sid, playlist);
	}
	const rows: SpotifyPanelRow<I>[] = [];
	const usedImported = new Set<string>();
	for (const playlist of account ?? []) {
		const match = importedBySpotifyId.get(playlist.spotify_id) ?? null;
		if (match !== null) usedImported.add(match.playlist_id);
		rows.push({
			playlist_id: match?.playlist_id ?? `${ACCOUNT_ROW_PREFIX}${playlist.spotify_id}`,
			pref_key: `spotify:${playlist.spotify_id}`,
			name: playlist.name,
			track_count: playlist.track_count,
			owned: playlist.owned,
			updated_at: match?.updated_at ?? null,
			open_url: playlist.open_url,
			account: playlist,
			imported: match
		});
	}
	for (const playlist of imported) {
		if (usedImported.has(playlist.playlist_id)) continue;
		const sid = importedSpotifyId(playlist);
		rows.push({
			playlist_id: playlist.playlist_id,
			pref_key: sid !== null ? `spotify:${sid}` : spotifyPreferenceKey(playlist.playlist_id),
			name: playlist.name,
			track_count: playlist.track_count,
			owned: false,
			updated_at: playlist.updated_at ?? null,
			open_url: sid === null ? null : `https://open.spotify.com/playlist/${sid}`,
			account: null,
			imported: playlist
		});
	}
	return rows;
}

/**
 * How many of the account's own playlists are imported. An imported playlist
 * the account no longer lists is a row, but not one of the account's, so it
 * is not counted: the tally never exceeds the account total.
 */
export function importedAccountCount(rows: readonly SpotifyPanelRow[]): number {
	return rows.filter((row) => row.account !== null && row.imported !== null).length;
}

/** #9685: open a row in the side player, then count it as recent. A refused or
 * blocked open throws before anything is recorded. */
export async function playSpotifyRow(
	row: Pick<SpotifyPanelRow, 'open_url' | 'pref_key'>,
	open: (url: string) => Promise<void>,
	remember: (prefKey: string) => void
): Promise<void> {
	if (row.open_url === null) return;
	await open(row.open_url);
	remember(row.pref_key);
}
