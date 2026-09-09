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
import type { AutoSyncPrefs, LastPlaylistPref, LevelCalibrationPrefs } from './prefs-types';

/** Bounds mirror `_CAL_MIN_DBFS`/`_CAL_MAX_DBFS`/`_CAL_CEILING_MAX_DBFS` in
 * `ui_prefs.py`: -60 is the meter's floor, +12 covers loudness-war true peaks
 * above 0 dBFS for the red anchor. ceiling_dbfs gets the tighter 0 dBFS upper
 * bound: min(1, 10**(dbfs/20)) is a no-op for any dbfs above 0, so a wider
 * range would let M read enabled while the master gain stays untouched.
 * Exported so a capture can be rejected at the source
 * (level-calibration-prefs.ts) instead of persisting a value this same
 * module's parser will refuse to load back, which would brick the whole
 * prefs singleton on next reload. */
export const CAL_MIN_DBFS = -60;
export const CAL_MAX_DBFS = 12;
export const CAL_CEILING_MAX_DBFS = 0;

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

/** Absent is the pre-#1475 first-run state and yields the defaults (never
 * calibrated); present but malformed throws, same as every other nested
 * field here. Mirrors `_parse_level_calibration` in `ui_prefs.py` exactly,
 * including the "enabling with no captured level" invariant. */
export function parseLevelCalibration(
	raw: unknown,
	storageKey: string,
	defaults: LevelCalibrationPrefs
): LevelCalibrationPrefs {
	if (raw === undefined) return { ...defaults };
	if (raw === null || typeof raw !== 'object') {
		throw new Error(
			`${storageKey}: malformed prefs blob (level_calibration must be an object) - ` +
				'clear the localStorage key to recover'
		);
	}
	const obj = raw as Partial<LevelCalibrationPrefs>;
	for (const key of ['red_dbfs', 'ceiling_dbfs'] as const) {
		const val = obj[key];
		if (val === undefined || val === null) continue;
		const max = key === 'ceiling_dbfs' ? CAL_CEILING_MAX_DBFS : CAL_MAX_DBFS;
		if (typeof val !== 'number' || !Number.isFinite(val) || val < CAL_MIN_DBFS || val > max) {
			throw new Error(
				`${storageKey}: malformed prefs blob (level_calibration.${key} must be a finite number ` +
					`between ${CAL_MIN_DBFS} and ${max}) - clear the localStorage key to recover`
			);
		}
	}
	for (const key of ['red_enabled', 'ceiling_enabled'] as const) {
		if (obj[key] !== undefined && typeof obj[key] !== 'boolean') {
			throw new Error(
				`${storageKey}: malformed prefs blob (level_calibration.${key} is not a boolean) - ` +
					'clear the localStorage key to recover'
			);
		}
	}
	const result: LevelCalibrationPrefs = {
		red_dbfs: obj.red_dbfs ?? defaults.red_dbfs,
		red_enabled: obj.red_enabled ?? defaults.red_enabled,
		ceiling_dbfs: obj.ceiling_dbfs ?? defaults.ceiling_dbfs,
		ceiling_enabled: obj.ceiling_enabled ?? defaults.ceiling_enabled
	};
	for (const [flag, level] of [
		['red_enabled', 'red_dbfs'],
		['ceiling_enabled', 'ceiling_dbfs']
	] as const) {
		if (result[flag] && result[level] === null) {
			throw new Error(
				`${storageKey}: malformed prefs blob (level_calibration.${flag} requires ${level} to be ` +
					'set) - clear the localStorage key to recover'
			);
		}
	}
	return result;
}
