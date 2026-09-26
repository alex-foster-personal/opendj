/**
 * Library browser contract: what the browser panel renders, plus the
 * GET /api/v1/tracks/{stable_id}/rb-meta wire payload that hydrates its rows
 * (COMPONENT-MAP 2.4) and the artwork size enum those rows fetch with.
 *
 * Split out of the former lib/rb/types.ts god module. The wire payload and
 * the row model share a module because `TrackRow` embeds `RbMeta` directly -
 * they are one concept (a library row and everything known about it), not
 * two that happen to be adjacent.
 */

import type { AnlzWaveformBands } from './anlz-types';

/** Optional analysis metadata reserved for future key and tempo-aware sync. */
export interface TrackAnalysisHints {
	dynamic_key?: boolean;
	dynamic_tempo?: boolean;
	musical_mode?: 'major' | 'minor';
	chord_progression?: string;
}

/** Artwork size enum for GET /tracks/{sid}/artwork (COMPONENT-MAP 2.2). */
export type ArtworkSize = 's' | 'm' | 'orig';

/** Venue-rung audio quality for one file (apps/shared/audio_quality.py).
 *
 * `venue`/`rank`/`kbps` are null when it could NOT be measured (missing
 * file, no duration, streaming URI, unknown container); `blurb` then
 * carries the reason. Render that as an explicit unknown - never fall
 * back to a guessed rung. */
export interface TrackQuality {
	/** Ladder key, e.g. 'warehouse'; null = unknown. */
	venue: string | null;
	/** Human label, e.g. 'Warehouse'; 'Unknown' when venue is null. */
	label: string;
	/** 0 (naughty step) .. 5 (stadium); null = unknown. */
	rank: number | null;
	/** Total rungs on the ladder (6), for the "rank of of" readout. */
	of: number;
	/** Rung description, or the reason it is unknown. */
	blurb: string;
	/** Effective kbps (size over duration), rounded; null = unknown. */
	kbps: number | null;
	/** Lowercased file extension including the dot, e.g. '.mp3'. */
	container: string;
	/** True when the container is a lossless one. */
	lossless: boolean;
}

/** The six ladder rungs, from GET /tracks/quality-ladder (legend source
 * of truth - the UI must not keep a second hardcoded copy). */
export interface QualityRung {
	rank: number;
	key: string;
	label: string;
	blurb: string;
}

/** A real, already-detected PQTZ field-vs-interval BPM disagreement (see
 * apps/webui/server/beatgrid_diagnostics.py). Sourced from a small sidecar
 * cache written the last time /anlz parsed this track's beatgrid - never
 * computed on the /rb-meta hot path (see server rb_vendor.cached_beatgrid_issue). */
export interface BeatgridIssue {
	severity: 'warning' | 'error';
	at_sec: number;
	field_bpm: number;
	interval_bpm: number;
	disagreement_bpm: number;
}

export interface RbMeta {
	/** 40-hex stable id. */
	stable_id: string;
	/** 'rekordbox' when the track has a vendor mapping, 'local' for a
	 * locally imported file (no djmdContent row): every rekordbox-sourced
	 * field below is then empty, never synthesised. */
	vendor: 'rekordbox' | 'local';
	/** djmdContent.ID in the decrypted master.plain.db; null when local. */
	vendor_id: string | null;
	/** True when the resolved FolderPath exists on disk. False for the 74%
	 * dead-path rows - a REAL library state the browser must show. */
	file_exists: boolean;
	/** True when FolderPath is a tidal:/soundcloud:/spotify: URI (cloud icon,
	 * load action inert). */
	is_streaming: boolean;
	/** Resolved absolute path or streaming URI (the state layer's own
	 * file_path when vendor is 'local'); null when the track has none. */
	folder_path: string | null;
	/** Genre column value from djmdContent join; null when unset. */
	genre: string | null;
	/** True/false once actually checked; null only for a local-vendor row
	 * (vendor: 'local') whose optional mutagen tag reader was never available
	 * to check with (#795) - an honest unknown, not a guessed false. A
	 * rekordbox-mapped row is always a definite true/false. Lets the browser
	 * skip doomed fetches when it IS false. */
	artwork_available: boolean | null;
	/** Why artwork is / isn't available (empty ImagePath is the common miss). */
	artwork_status: 'ok' | 'no_image_path' | 'unresolved' | 'file_missing';
	/** True when the ANLZ dir exists - ditto for /anlz. */
	analysis_available: boolean;
	/** Cached beatgrid data-quality verdict; null when clean OR never yet
	 * evaluated (no /anlz fetch has happened for this track). */
	beatgrid_issue: BeatgridIssue | null;
	/** Live djmdCue row count for the track. */
	cue_count: number;
	/** Venue-rung quality from the same stat that answered file_exists. */
	quality: TrackQuality;
}

/** One hydrated row in the browser track list. Field order mirrors the
 * screenshot columns (SCREENSHOT-SPEC 5c). */
export interface TrackRow {
	/** 40-hex stable id. */
	stable_id: string;
	/** 1-based membership position within the pane playlist (# column). */
	order: number;
	/** Track Title column; null renders empty. */
	title: string | null;
	/** Artist column; null renders empty. */
	artist: string | null;
	/** K column - Camelot key; null renders empty. */
	key: string | null;
	/** Own-lane key read model for failed/missing tooltips (NATIVE-04). */
	key_status?: 'ok' | 'failed' | 'missing' | 'available-not-selected';
	bpm_status?: 'ok' | 'failed' | 'missing' | 'available-not-selected';
	bpm_reason?: string | null;
	key_reason?: string | null;
	loudness_status?: 'ok' | 'failed' | 'missing' | 'available-not-selected';
	loudness_reason?: string | null;
	/** B column - BPM; null renders empty. */
	bpm: number | null;
	/** Analyzer-only dynamic key/tempo facts. Absence is explicitly not analyzed. */
	analysis_hints?: TrackAnalysisHints;
	/** Rating column 0..5 stars, editable via existing PATCH + If-Match etag. */
	rating: number | null;
	/** Current etag for optimistic-concurrency PATCH; from GET /tracks/{sid}. */
	etag: string;
	/** Comments column (TrackOut.notes); mostly empty, matches screenshot. */
	comments: string | null;
	/** Time column source in ms; render MM:SS. null renders empty. */
	duration_ms: number | null;
	/** rb-meta payload once fetched (cloud icon, missing-file state, Genre
	 * column); null while not yet loaded. */
	rb_meta: RbMeta | null;
	/** Preview column waveform once lazily fetched; null = not loaded yet.
	 * 'unavailable' = /anlz said no analysis - render the empty strip. */
	preview: AnlzWaveformBands | 'unavailable' | null;
}

/** One node in the playlist tree panel (5b). state.db has NO folder
 * hierarchy, so v1 produces: one 'all_tracks' node + flat 'playlist' nodes. */
export interface PlaylistNode {
	/** Playlist id from PlaylistSummary; 'all' for the All Tracks node.
	 * 'missing' is the All-Tracks-style Missing Tracks sentinel, not a
	 * server playlist id. */
	playlist_id: string;
	/** Display name. */
	name: string;
	/** Right-aligned track count - OUR real count, never the screenshot's. */
	track_count: number;
	/** Broken tracks omitted from the displayed playable count. */
	broken_count: number;
	/** Node flavour; 'folder' reserved for future hierarchy, unused v1.
	 * 'missing_tracks' is the reserved Missing Tracks view (playlist_id
	 * 'missing'), never matched by a user playlist's display name. */
	kind: 'all_tracks' | 'playlist' | 'smartlist' | 'folder' | 'missing_tracks' | 'taglist' | 'autolist';
	/** Below server min-available ratio; tree row renders dimmed. */
	mostly_broken?: boolean;
	/** When true, extra copies of an already-present track are rejected on add. */
	forbid_duplicates?: boolean;
	/** Children for folder nodes; always [] at v1. */
	children: PlaylistNode[];
}
