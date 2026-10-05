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
import { RbApiError } from './api-rb-error';
import { refuseStickRead } from './track-source';
import type { AnlzBeatgrid } from './anlz-types';

/** GET /tracks/{sid}/beatgrid-fallback response (analysis.py::BeatgridFallbackOut).
 * Kept hand-written: generated BeatgridFallbackOut.beatgrid is FallbackBeatgridOut,
 * while callers consume AnlzBeatgrid from './anlz-types'. */
export interface BeatgridFallbackOut {
	stable_id: string;
	source: string;
	backend: string;
	backend_version: string;
	bpm: number;
	bpm_confidence: number;
	/** Whether the authoritative rekordbox ANLZ grid also exists. Normally
	 * false, since callers only reach this endpoint after /anlz reports
	 * ANALYSIS_NOT_FOUND - true means a vendor mapping landed WHILE this
	 * deferred request was in flight, so beatgrid-upgrade.ts's caller must
	 * not install this synthetic grid over the now-available real one. */
	anlz_available: boolean;
	/** Exactly the /anlz beatgrid shape - see AnlzBeatgrid. */
	beatgrid: AnlzBeatgrid;
}

export async function fetchBeatgridFallback(stable_id: string): Promise<BeatgridFallbackOut> {
	// Spec 4b: a stick grid is the stick's own PQTZ; no library fallback exists.
	refuseStickRead(stable_id, 'beatgrid fallback');
	try {
		return (await unwrap(
			api.GET('/api/v1/tracks/{stable_id}/beatgrid-fallback', {
				params: { path: { stable_id } }
			})
		)) as BeatgridFallbackOut;
	} catch (error) {
		if (error instanceof ApiError) {
			// error.body carries the 404's own anlz_available, distinct from
			// the 200 response's field of the same name: a vendor mapping can
			// land while apps.analysis still has nothing usable, so the
			// backend answers BEATGRID_FALLBACK_NOT_FOUND on this path too -
			// see beatgrid-upgrade.ts's catch block.
			throw new RbApiError(error.status, error.code, error.message, error.body);
		}
		throw error;
	}
}
