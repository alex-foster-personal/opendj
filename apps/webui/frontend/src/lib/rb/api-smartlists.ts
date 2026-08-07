/**
 * Typed fetch client for the smartlists read router
 * (LANE smartlists-router -- apps/webui/server/routes/smartlists.py).
 *
 * Lives in its OWN module: api-rb.ts and types.ts are wave hotspots owned
 * by the integrator, so smartlist types + fetchers are exported from here
 * and rule-editor-ui / browser integrators import from this file.
 *
 * Fail-fast: same RbApiError contract as api-rb.ts -- every non-ok
 * response throws the backend's explicit {"detail": {code, message}}.
 * Known codes: SMARTLISTS_DB_UNAVAILABLE (503), SMARTLIST_NOT_FOUND (404),
 * SMARTLIST_EVAL_UNAVAILABLE (503), SMARTLIST_RULE_INVALID (500),
 * SMARTLIST_MEMBER_MISSING (500).
 */

import { RB_API_BASE, RbApiError, type StemSummary, type Vocals } from './api-rb';

// ----------------------------------------------------------- types

/** Rule AST node: predicate {field, op, value} or logical {op, children}. */
export type SmartlistRule =
	| { field: string; op: string; value: unknown }
	| { op: 'and' | 'or' | 'not'; children: SmartlistRule[] };

/** GET /smartlists list row / GET /smartlists/{id}. */
export interface SmartlistSummary {
	id: string;
	name: string;
	rule: SmartlistRule;
	/** One-line human rendering of the AST, for tree tooltips/subtitles. */
	rule_summary: string;
	order_by: string;
	referenced_fields: string[];
	rule_schema_version: number;
	last_evaluated_at: string | null;
	created_at: string;
	modified_at: string;
}

/** One hydrated row -- field-for-field the playlist-detail TrackRowOut
 * shape, so browser table components render either without branching. */
export interface SmartlistTrackRow {
	stable_id: string;
	title: string | null;
	artist: string | null;
	key: string | null;
	bpm: number | null;
	rating: number | null;
	duration_ms: number | null;
	genre: string | null;
	comments: string | null;
	etag: string;
	preview_b64: string | null;
	preview_max: number | null;
	file_exists: boolean;
	is_streaming: boolean;
	vocals: Vocals;
	stems: StemSummary;
}

/** GET /smartlists/{id}/tracks -- live evaluation result. */
export interface SmartlistTracks {
	smartlist_id: string;
	name: string;
	rule_summary: string;
	order_by: string;
	/** Full ordered membership (evaluator order). */
	items: string[];
	/** Hydrated rows in the same order as items. */
	tracks: SmartlistTrackRow[];
}

// ----------------------------------------------------------- _helpers

async function _fetchJson<T>(path: string): Promise<T> {
	const r = await fetch(`${RB_API_BASE}${path}`, { headers: { Accept: 'application/json' } });
	if (!r.ok) {
		const body = (await r.json()) as { detail?: { code?: string; message?: string } | string };
		const detail = typeof body.detail === 'object' && body.detail !== null ? body.detail : undefined;
		throw new RbApiError(r.status, detail?.code ?? `HTTP_${r.status}`, detail?.message ?? r.statusText);
	}
	return (await r.json()) as T;
}

// ----------------------------------------------------------- fetchers

export async function listSmartlists(): Promise<SmartlistSummary[]> {
	return _fetchJson<SmartlistSummary[]>('/api/v1/smartlists');
}

export async function getSmartlist(id: string): Promise<SmartlistSummary> {
	return _fetchJson<SmartlistSummary>(`/api/v1/smartlists/${encodeURIComponent(id)}`);
}

export async function getSmartlistTracks(id: string, limit?: number): Promise<SmartlistTracks> {
	const qs = limit !== undefined ? `?limit=${limit}` : '';
	return _fetchJson<SmartlistTracks>(`/api/v1/smartlists/${encodeURIComponent(id)}/tracks${qs}`);
}
