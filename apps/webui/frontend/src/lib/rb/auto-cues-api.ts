/**
 * Fetch client for GET /tracks/{sid}/auto-cues - the analysis-router lane
 * contract (apps/webui/server/routes/analysis.py). Items are PROPOSALS,
 * never committed djmdCue hot cues. This file is a consumer; do not edit
 * `api-rb.ts`.
 *
 * CONVERTED onto the generated OpenAPI client (`src/lib/api/client.ts`).
 * Deliberately NOT routed through api-rb: that file is a single-owner-per-
 * wave hotspot (CLAUDE.md), so this lane's frontend keeps its own thin
 * wrapper rather than editing it.
 */
import { ApiError, api, unwrap } from '../api/client';
import type { components } from '../api-types';
import { RbApiError } from './api-rb';
import { refuseStickRead } from './track-source';

export type AutoCuesOut = components['schemas']['AutoCuesOut'];
export type AutoCueOut = components['schemas']['AutoCueOut'];
export { RbApiError };

export async function fetchAutoCues(stable_id: string): Promise<AutoCuesOut> {
	// Spec 4b: auto-cues are library analysis; no stick route exists.
	refuseStickRead(stable_id, 'auto-cues');
	try {
		return await unwrap(
			api.GET('/api/v1/tracks/{stable_id}/auto-cues', {
				params: { path: { stable_id } }
			})
		);
	} catch (error) {
		if (error instanceof ApiError) {
			throw new RbApiError(error.status, error.code, error.message, error.body);
		}
		throw error;
	}
}
