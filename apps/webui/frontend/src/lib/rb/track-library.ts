/**
 * Track library lifecycle client (LIBM-52).
 *
 * Kept out of api-rb.ts (single-owner hotspot per FANOUT-CONVENTIONS.md).
 */

import { api, unwrap } from '../api/rb-http';
import { refuseStickWrite } from './track-source';

export interface TrackMembershipRef {
	playlist_id: string;
	position: number;
}

export interface TrackLifecycleOut {
	stable_id: string;
	deleted_at: string | null;
	memberships: TrackMembershipRef[];
}

/** Restore a tombstoned track and the playlist memberships its removal stamped (LIBM-54). */
export async function undeleteFromLibrary(stableId: string): Promise<TrackLifecycleOut> {
	// Spec 4b: a stick track is never in the library, so there is nothing to restore.
	refuseStickWrite(stableId, 'restore to library');
	return await unwrap(
		api.POST('/api/v1/tracks/{stable_id}:undelete', {
			params: { path: { stable_id: stableId } }
		})
	);
}

export async function removeFromLibrary(stableId: string): Promise<TrackLifecycleOut> {
	// Spec 4b: a stick track is not in the library, and nothing is written for it.
	refuseStickWrite(stableId, 'remove from library');
	return await unwrap(
		api.POST('/api/v1/tracks/{stable_id}:remove', {
			params: { path: { stable_id: stableId } }
		})
	);
}
