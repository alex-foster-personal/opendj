/**
 * Storage-shape types shared between prefs.svelte.ts (the reactive singleton)
 * and prefs-fields.ts (its nested-object parsers), split into their own
 * dependency-free leaf module so the two do not import each other.
 *
 * Plain module (no runes, no other imports): just the nested-object
 * shapes that prefs-fields.ts validates.
 */

/** Preferred vendor writeback targets (preference only; CLI writeback today). */
export interface AutoSyncPrefs {
	rekordbox: boolean;
	djay: boolean;
	open_dj: boolean;
}

/** Persisted pane identity. Mirrors BootPlaylistChoice in the pane contract,
 * declared here so prefs owns its own storage shape rather than importing a
 * component module into the prefs layer. */
export interface LastPlaylistPref {
	playlist_id: string;
	name: string;
	kind: 'all_tracks' | 'playlist';
}

/**
 * By-ear level calibration (#1475), mirrors `LevelCalibrationOut` in
 * `apps/webui/server/routes/ui_prefs.py`. `red_dbfs` anchors the channel
 * meter's first red segment; `ceiling_dbfs` is the master output ceiling.
 * Independent: either can be captured and toggled without the other, and a
 * captured number survives its own `*_enabled` going false.
 */
export interface LevelCalibrationPrefs {
	red_dbfs: number | null;
	red_enabled: boolean;
	ceiling_dbfs: number | null;
	ceiling_enabled: boolean;
}

/** Spotify source-panel pin + recent memory (#315). localStorage only. */
export interface SpotifyLibraryPref {
	pinned_ids: string[];
	recent_ids: string[];
}
