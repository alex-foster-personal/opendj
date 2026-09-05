/**
 * Storage-shape types shared between prefs.svelte.ts (the reactive singleton)
 * and prefs-fields.ts (its nested-object parsers), split into their own
 * dependency-free leaf module so the two do not import each other.
 *
 * Plain module (no runes, no other imports): just the two nested-object
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
