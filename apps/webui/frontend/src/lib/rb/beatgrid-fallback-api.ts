/**
 * Fetch client for GET /tracks/{sid}/beatgrid-fallback - the anlz-fallback-
 * beatgrid lane contract (apps/webui/server/routes/analysis.py, owned by
 * the analysis-router lane). ANLZ is always preferred: callers only reach
 * for this endpoint after GET /anlz has reported ANALYSIS_NOT_FOUND.
 *
 * CONVERTED onto the generated OpenAPI client (`src/lib/api/client.ts`).
 * Deliberately NOT routed through api-rb.ts: that file is a single-owner-
 * per-wave hotspot (CLAUDE.md), so this lane's frontend keeps its own
 * thin wrapper rather than editing it.
 */
import { ApiError, api, unwrap } from '../api/client';
import { RbApiError } from './api-rb';
import type { AnlzBeatgrid } from './types';

/** GET /tracks/{sid}/beatgrid-fallback response (analysis.py::BeatgridFallbackOut).
 * Kept hand-written: generated BeatgridFallbackOut.beatgrid is FallbackBeatgridOut,
 * while callers consume AnlzBeatgrid from './types'. */
export interface BeatgridFallbackOut {
	stable_id: string;
	source: string;
	backend: string;
	backend_version: string;
	bpm: number;
	bpm_confidence: number;
	/** Whether the authoritative rekordbox ANLZ grid also exists - true here
	 * would mean the caller should have preferred /anlz and never reached
	 * this endpoint; kept for honesty/debugging, not for UI branching. */
	anlz_available: boolean;
	/** Exactly the /anlz beatgrid shape - see AnlzBeatgrid. */
	beatgrid: AnlzBeatgrid;
}

export async function fetchBeatgridFallback(stable_id: string): Promise<BeatgridFallbackOut> {
	try {
		return (await unwrap(
			api.GET('/api/v1/tracks/{stable_id}/beatgrid-fallback', {
				params: { path: { stable_id } }
			})
		)) as BeatgridFallbackOut;
	} catch (error) {
		if (error instanceof ApiError) {
			throw new RbApiError(error.status, error.code, error.message);
		}
		throw error;
	}
}
