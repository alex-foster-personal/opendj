/**
 * Fail-fast parsers for the auto_sync + last_playlist fields of the
 * localStorage prefs blob, split out of prefs.svelte.ts's _load() (a
 * distinct concern from the reactive singleton and the primitive-field
 * checks that stay inline there - both of these validate a nested object
 * shape rather than a bare primitive).
 *
 * Plain module (no runes): pure parse functions over an already-parsed
 * blob, so they need no .svelte.ts extension.
 */
import type { AutoSyncPrefs, LastPlaylistPref } from './prefs-types';

export function parseAutoSync(
	raw: unknown,
	storageKey: string,
	defaults: AutoSyncPrefs
): AutoSyncPrefs {
	if (raw === undefined) return { ...defaults };
	if (raw === null || typeof raw !== 'object') {
		throw new Error(
			`${storageKey}: malformed prefs blob (auto_sync must be an object) - ` +
				'clear the localStorage key to recover'
		);
	}
	const obj = raw as Partial<AutoSyncPrefs>;
	for (const key of ['rekordbox', 'djay', 'open_dj'] as const) {
		if (obj[key] !== undefined && typeof obj[key] !== 'boolean') {
			throw new Error(
				`${storageKey}: malformed prefs blob (auto_sync.${key} is not a boolean) - ` +
					'clear the localStorage key to recover'
			);
		}
	}
	return {
		rekordbox: obj.rekordbox ?? defaults.rekordbox,
		djay: obj.djay ?? defaults.djay,
		open_dj: obj.open_dj ?? defaults.open_dj
	};
}

/** Absent (old blob written before this field existed) is the real first-run
 * state and yields null; present but the wrong shape throws, same as every
 * other field here - a half-valid pane identity would restore into a load
 * against an id that is not a string. */
export function parseLastPlaylist(raw: unknown, storageKey: string): LastPlaylistPref | null {
	if (raw === undefined || raw === null) return null;
	if (typeof raw !== 'object') {
		throw new Error(
			`${storageKey}: malformed prefs blob (last_playlist must be an object or null) - ` +
				'clear the localStorage key to recover'
		);
	}
	const obj = raw as Partial<LastPlaylistPref>;
	if (typeof obj.playlist_id !== 'string' || obj.playlist_id === '') {
		throw new Error(
			`${storageKey}: malformed prefs blob (last_playlist.playlist_id must be a non-empty string) - ` +
				'clear the localStorage key to recover'
		);
	}
	if (typeof obj.name !== 'string') {
		throw new Error(
			`${storageKey}: malformed prefs blob (last_playlist.name must be a string) - ` +
				'clear the localStorage key to recover'
		);
	}
	if (obj.kind !== 'all_tracks' && obj.kind !== 'playlist') {
		throw new Error(
			`${storageKey}: malformed prefs blob (last_playlist.kind must be 'all_tracks'|'playlist') - ` +
				'clear the localStorage key to recover'
		);
	}
	return { playlist_id: obj.playlist_id, name: obj.name, kind: obj.kind };
}
