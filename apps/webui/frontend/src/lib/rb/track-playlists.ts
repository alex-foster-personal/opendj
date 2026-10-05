/**
 * Track playlist reverse lookup client (LIBM-29).
 *
 * Kept out of api-rb.ts (single-owner hotspot per FANOUT-CONVENTIONS.md).
 */

import { api, unwrap } from '../api/client';
import { refuseStickRead } from './track-source';

export interface TrackPlaylistHit {
	playlist_id: string;
	name: string;
	vendor: string;
	positions: number[];
}

export async function listTrackPlaylists(stableId: string): Promise<TrackPlaylistHit[]> {
	// Spec 4b: library playlist membership has no stick route.
	refuseStickRead(stableId, 'library playlists');
	return await unwrap(
		api.GET('/api/v1/tracks/{stable_id}/playlists', {
			params: { path: { stable_id: stableId } }
		})
	);
}
