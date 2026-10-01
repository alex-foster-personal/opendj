/**
 * Typed client calls for the reconcile-router LANE (read-only broken-track
 * listing + relocate-files candidate finder/apply). Kept as a dedicated file
 * rather than extending `api.ts` so the two lane members building on this
 * contract in parallel (missing-tracks-folder, relocate-files) don't collide
 * on the same file.
 *
 * CONVERTED onto the generated OpenAPI client (`src/lib/api/client.ts`); the
 * exported function signatures are unchanged, so call sites did not move.
 * The row types below are aliases of the generated schemas: the server's
 * OpenAPI document is the only definition, so a backend field rename fails
 * `pnpm run check` here instead of drifting silently.
 */

import type { components } from './api-types';
import { ApiError, api, unwrap } from './api/client';

export type BrokenTrack = components['schemas']['BrokenTrackOut'];
export type BrokenTrackList = components['schemas']['BrokenTrackList'];
export type RelocateCandidate = components['schemas']['RelocateCandidateOut'];
export type RelocateCandidateList = components['schemas']['RelocateCandidateList'];
export type RelocateApplyResult = components['schemas']['RelocateApplyOut'];

export class RelocateApplyError extends Error {
	constructor(
		public status: number,
		public code: string,
		message: string
	) {
		super(message);
	}
}

export async function listBroken(playlistId?: string): Promise<BrokenTrackList> {
	return unwrap(
		api.GET('/api/v1/reconcile/broken', {
			params: { query: { playlist_id: playlistId ?? null } }
		})
	);
}

/**
 * One page of the broken listing. `total` is always the whole count and
 * `next_offset` is null on the last page. The route hydrates only the rows it
 * returns, so a page costs a fraction of the unpaged call above.
 */
export async function listBrokenPage(limit: number, offset: number): Promise<BrokenTrackList> {
	return unwrap(
		api.GET('/api/v1/reconcile/broken', {
			params: { query: { limit, offset } }
		})
	);
}

export async function getRelocateCandidates(
	stableId: string,
	limit = 5
): Promise<RelocateCandidateList> {
	return unwrap(
		api.GET('/api/v1/relocate/candidates/{stable_id}', {
			params: { path: { stable_id: stableId }, query: { limit } }
		})
	);
}

export async function applyRelocate(
	stableId: string,
	newPath: string,
	opts: {
		ifMatch: string;
		expectedOriginalPath: string;
		expectedVendorId: string | null;
		expectedCandidateIdentity: string;
	}
): Promise<RelocateApplyResult> {
	try {
		return await unwrap(
			api.POST('/api/v1/relocate/{stable_id}/apply', {
				params: { path: { stable_id: stableId }, header: { 'If-Match': opts.ifMatch } },
				body: {
					new_path: newPath,
					expected_candidate_identity: opts.expectedCandidateIdentity,
					expected_original_path: opts.expectedOriginalPath,
					expected_vendor_id: opts.expectedVendorId,
					confirm: true
				}
			})
		);
	} catch (error) {
		// This write is irreversible, so the route's own {code, message} is
		// surfaced verbatim to the operator rather than a bare status.
		if (error instanceof ApiError) {
			throw new RelocateApplyError(error.status, error.code, error.message);
		}
		throw error;
	}
}
