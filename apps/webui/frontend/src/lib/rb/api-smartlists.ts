/**
 * Typed client for the smartlists read router
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
 * SMARTLIST_MEMBER_MISSING (500), SMARTLIST_NAME_CONFLICT (409).
 *
 * CONVERTED onto the generated OpenAPI client (src/lib/api/client.ts).
 * Transport only: the exported types below stay hand-written rather than
 * aliasing the generated schemas, because generated SmartlistSummary types
 * `rule` as an untyped {[key: string]: unknown} map while this module
 * keeps the SmartlistRule AST union the rule editor depends on.
 */

import {
	RbApiError,
	type CloudTransferWire,
	type FileAvailabilityStatus,
	type StemSummary,
	type Vocals
} from './api-rb';
import type { TrackQuality } from './library-types';

import { ApiError, api, unwrap } from '../api/client';

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
	/** Live membership size when requested; null when omitted or evaluation failed. */
	count: number | null;
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
	energy: number | null;
	energy_source: 'mik' | null;
	energy_reason: string;
	duration_ms: number | null;
	genre: string | null;
	genre_reason?: string | null;
	comments: string | null;
	etag: string;
	preview_b64: string | null;
	preview_max: number | null;
	/** PERF-RB-01: null only while file_availability is AVAILABILITY_PENDING. */
	file_exists: boolean | null;
	file_availability: FileAvailabilityStatus;
	is_streaming: boolean;
	is_remote?: boolean;
	/** LIBUX-13: a recorded remote copy, including when local audio also exists. */
	has_remote_copy?: boolean;
	/** LIBUX-13: present only while this engine process is moving real bytes. */
	cloud_transfer?: CloudTransferWire | null;
	spotify_pending: boolean;
	quality: TrackQuality;
	play_count: number;
	vocals: Vocals;
	stems: StemSummary;
	has_rb_mapping: boolean;
	artwork_available: boolean | null;
	artwork_status: 'ok' | 'no_image_path' | 'unresolved' | 'file_missing';
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

/** Keep this module's error contract after the conversion: a daemon answer
 * still throws RbApiError with the route's {code, message} (ApiError already
 * decoded the detail envelope, with the same HTTP_<status>/statusText
 * fallbacks the old _fetchJson applied); anything else rethrows untouched. */
function _throwSmartlistError(error: unknown): never {
	if (error instanceof ApiError) {
		throw new RbApiError(error.status, error.code, error.message);
	}
	throw error;
}

// ----------------------------------------------------------- fetchers

export async function listSmartlists(
	options: { includeCounts?: boolean } = {}
): Promise<SmartlistSummary[]> {
	try {
		const data = await unwrap(
			api.GET('/api/v1/smartlists', {
				params: {
					query: options.includeCounts === true ? { include_counts: true } : {}
				}
			})
		);
		return data as unknown as SmartlistSummary[];
	} catch (error) {
		_throwSmartlistError(error);
	}
}

export async function getSmartlist(id: string): Promise<SmartlistSummary> {
	try {
		const data = await unwrap(
			api.GET('/api/v1/smartlists/{smartlist_id}', { params: { path: { smartlist_id: id } } })
		);
		return data as unknown as SmartlistSummary;
	} catch (error) {
		_throwSmartlistError(error);
	}
}

export async function getSmartlistTracks(id: string, limit?: number): Promise<SmartlistTracks> {
	try {
		const data = await unwrap(
			api.GET('/api/v1/smartlists/{smartlist_id}/tracks', {
				params: { path: { smartlist_id: id }, query: { limit: limit ?? null } }
			})
		);
		return data as unknown as SmartlistTracks;
	} catch (error) {
		_throwSmartlistError(error);
	}
}

/** DELETE /smartlists/{id} -> 204, so no unwrap: middleware throws on non-2xx. */
export async function deleteSmartlist(id: string): Promise<void> {
	try {
		await api.DELETE('/api/v1/smartlists/{smartlist_id}', {
			params: { path: { smartlist_id: id } }
		});
	} catch (error) {
		_throwSmartlistError(error);
	}
}

export async function createSmartlist(body: {
	name: string;
	rule: SmartlistRule;
	order_by?: string;
}): Promise<SmartlistSummary> {
	try {
		const data = await unwrap(
			api.POST('/api/v1/smartlists', { body })
		);
		return data as unknown as SmartlistSummary;
	} catch (error) {
		_throwSmartlistError(error);
	}
}

export async function getSmartlistWithEtag(
	id: string
): Promise<{ summary: SmartlistSummary; etag: string }> {
	let data: unknown;
	let response: Response;
	try {
		({ data, response } = await api.GET('/api/v1/smartlists/{smartlist_id}', {
			params: { path: { smartlist_id: id } }
		}));
	} catch (error) {
		_throwSmartlistError(error);
	}
	const etag = response.headers.get('etag');
	if (!etag) {
		throw new Error(`smartlist ${id}: GET response carries no ETag header`);
	}
	return { summary: data as unknown as SmartlistSummary, etag };
}

export async function updateSmartlist(
	id: string,
	body: { rule: SmartlistRule; name?: string; order_by?: string },
	etag: string
): Promise<SmartlistSummary> {
	try {
		const data = await unwrap(
			api.PUT('/api/v1/smartlists/{smartlist_id}', {
				params: { path: { smartlist_id: id }, header: { 'If-Match': etag } },
				body
			})
		);
		return data as unknown as SmartlistSummary;
	} catch (error) {
		_throwSmartlistError(error);
	}
}

export async function duplicateSmartlist(
	id: string,
	name?: string
): Promise<SmartlistSummary> {
	try {
		const data = await unwrap(
			api.POST('/api/v1/smartlists/{smartlist_id}/duplicate', {
				params: { path: { smartlist_id: id } },
				body: name === undefined ? {} : { name }
			})
		);
		return data as unknown as SmartlistSummary;
	} catch (error) {
		_throwSmartlistError(error);
	}
}
