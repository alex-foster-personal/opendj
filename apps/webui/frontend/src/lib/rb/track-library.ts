/**
 * Track library lifecycle client (LIBM-52).
 *
 * Kept out of api-rb.ts (single-owner hotspot per FANOUT-CONVENTIONS.md).
 */

import { api, unwrap } from '../api/client';
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

export async function removeFromLibrary(stableId: string): Promise<TrackLifecycleOut> {
	// Spec 4b: a stick track is not in the library, and nothing is written for it.
	refuseStickWrite(stableId, 'remove from library');
	return await unwrap(
		api.POST('/api/v1/tracks/{stable_id}:remove', {
			params: { path: { stable_id: stableId } }
		})
	);
}
