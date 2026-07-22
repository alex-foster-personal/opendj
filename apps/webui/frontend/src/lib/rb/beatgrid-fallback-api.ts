/**
 * Fetch client for GET /tracks/{sid}/beatgrid-fallback - the anlz-fallback-
 * beatgrid lane contract (apps/webui/server/routes/analysis.py, owned by
 * the analysis-router lane). ANLZ is always preferred: callers only reach
 * for this endpoint after GET /anlz has reported ANALYSIS_NOT_FOUND.
 *
 * Deliberately NOT routed through api-rb.ts: that file is a single-owner-
 * per-wave hotspot (CLAUDE.md), so this lane's frontend duplicates the few
 * lines of fetch/error plumbing rather than editing it.
 */
import { RB_API_BASE, RbApiError } from './api-rb';
import type { AnlzBeatgrid } from './types';

/** GET /tracks/{sid}/beatgrid-fallback response (analysis.py::BeatgridFallbackOut). */
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
	const r = await fetch(
		`${RB_API_BASE}/api/v1/tracks/${encodeURIComponent(stable_id)}/beatgrid-fallback`,
		{ headers: { Accept: 'application/json' } }
	);
	if (!r.ok) {
		const body = (await r.json()) as { detail?: { code?: string; message?: string } | string };
		const detail = typeof body.detail === 'object' && body.detail !== null ? body.detail : undefined;
		throw new RbApiError(
			r.status,
			detail?.code ?? `HTTP_${r.status}`,
			detail?.message ?? r.statusText
		);
	}
	return (await r.json()) as BeatgridFallbackOut;
}
