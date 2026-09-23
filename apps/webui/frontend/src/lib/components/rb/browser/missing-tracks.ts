/**
 * Reserved Missing Tracks tree folder (issue #173).
 *
 * Identity is the All-Tracks-style sentinel `playlist_id: 'missing'`, never a
 * coincidentally named user playlist. Fetch + row mapping live here so
 * PlaylistTree and pane-contract stay under the frontend 600-line ratchet.
 */
import type { BrokenTrack } from '$lib/reconcile-api';
import { listBroken } from '$lib/reconcile-api';
import type { PlaylistNode } from '$lib/rb/library-types';
import type { BrowserRow } from './pane-contract.svelte';

export const MISSING_TRACKS_ID = 'missing';
export const MISSING_TRACKS_NAME = 'Missing Tracks';
export const MISSING_TRACKS_ENERGY_REASON = 'not on broken listing';

export function isMissingTracksId(id: string | null): boolean {
	return id === MISSING_TRACKS_ID;
}

export function missingTracksNode(brokenCount: number): PlaylistNode {
	return {
		playlist_id: MISSING_TRACKS_ID,
		name: MISSING_TRACKS_NAME,
		track_count: brokenCount,
		broken_count: brokenCount,
		kind: 'missing_tracks',
		children: []
	};
}

export function brokenTrackToBrowserRow(track: BrokenTrack, order: number): BrowserRow {
	return {
		stable_id: track.stable_id,
		item_id: null,
		order,
		title: track.title ?? null,
		artist: track.artist ?? null,
		key: track.key ?? null,
		bpm: track.bpm ?? null,
		rating: track.rating ?? null,
		energy: null,
		energy_source: null,
		energy_reason: MISSING_TRACKS_ENERGY_REASON,
		etag: '',
		comments: null,
		duration_ms: track.duration_ms ?? null,
		genre: null,
		file_exists: false,
		file_availability: 'absent',
		quality: null,
		play_count: 0,
		is_streaming: false,
		strip: null,
		vocals: { status: 'not_analyzed' },
		stems: { status: 'none' },
		has_rb_mapping: track.vendor_id != null && track.vendor_id !== '',
		artwork_available: null,
		artwork_status: 'file_missing',
		rb_meta: null,
		revealed: false,
		match_context: null,
		lyrics: null,
		is_remix: null,
		is_radio_edit: null
	};
}

export async function fetchMissingTrackRows(): Promise<{
	rows: BrowserRow[];
	truncated: false;
	etag: '';
}> {
	const listing = await listBroken();
	return {
		rows: listing.tracks.map((track, index) => brokenTrackToBrowserRow(track, index + 1)),
		truncated: false,
		etag: ''
	};
}
