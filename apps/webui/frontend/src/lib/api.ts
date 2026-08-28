/**
 * Typed calls over the music-dj-tools FastAPI daemon: tracks, playlists,
 * PLAY IT, pairings, queues, settings, smartlists and health.
 *
 * CONVERTED onto the generated OpenAPI client (`src/lib/api/client.ts`). The
 * local `request()` helper is gone, every path is now the schema's own literal,
 * and `API_BASE` is a re-export of the client's copy -- this module was the
 * last holder of a second, independent base-URL resolution, so there is now
 * exactly one. Exported signatures, error classes and thrown messages are
 * unchanged; no call site moved.
 *
 * The response interfaces below stay hand-written on purpose. Most have already
 * drifted from the generated schemas -- `TracksPage.items` is `TrackListItemOut`
 * there, `PlaylistDetail` carries `tracks` and no `updated_at`,
 * `PlaylistSummary` gained `available_count`, `HealthOut.status` is an open
 * string, and `SmartlistOut.rule` is a typed `RuleAst` here against an open
 * record there. Aliasing them to `components['schemas'][...]` would therefore
 * change what every call site sees, which step 4 of CONVERSION-PATTERN.md
 * explicitly forbids. Each `as unknown as` at the client boundary marks one
 * such drift to close later; it is not a shortcut around a type error.
 */
import type { paths } from './api-types';
import { ApiError, api, unwrap } from './api/client';
import type { RuleAst } from './smartlists/rule-form';

export { API_BASE } from './api/client';

/** The documented `/api/v1/tracks` filter set. `listTracks` keeps its open
 * `Record` signature (call sites pass filter bags straight through), so the
 * bag is asserted onto this at the one point it reaches the client. */
type TrackListQuery = NonNullable<paths['/api/v1/tracks']['get']['parameters']['query']>;

export interface Track {
	stable_id: string;
	title: string | null;
	artist: string | null;
	album: string | null;
	bpm: number | null;
	key: string | null;
	/** From TrackOut.duration_ms. Null when the file has never been probed. */
	duration_ms: number | null;
	rating: number | null;
	tags: string[];
	notes: string | null;
	last_played_at: string | null;
	/** Rekordbox DJPlayCount when hydrated; 0 if unknown. */
	play_count?: number;
	created_at: string;
	updated_at: string;
	provenance: Record<string, { value: unknown; source: string; confidence: number | null; modified_at: string }>;
}

export interface TracksPage {
	items: Track[];
	next_cursor: string | null;
}

export interface PlaylistSummary {
	playlist_id: string;
	name: string;
	vendor: string;
	track_count: number;
	updated_at: string;
	/** Rekordbox custom tree position (flattened ParentID/Seq walk);
	 * null for non-rekordbox playlists - never invent an order for those. */
	seq: number | null;
}

export interface PlaylistDetail extends Omit<PlaylistSummary, 'track_count' | 'seq'> {
	items: string[];
	diff: {
		rb_only: string[];
		djay_only: string[];
		both: string[];
		conflicts: { stable_id: string; rb_position: number; djay_position: number }[];
	};
}

export interface Pairing {
	pairing_id: string;
	from_stable_id: string;
	to_stable_id: string;
	direction: '->' | '<->';
	source: 'manual' | 'learned' | 'ai';
	notes: string | null;
	created_at: string;
	updated_at: string;
}

export interface QueueItem {
	stable_id: string;
	kind: string;
	payload: Record<string, unknown>;
}

export interface QueueOut {
	items: QueueItem[];
	note: string | null;
}

export interface SettingItem {
	key: string;
	value: unknown;
	tbd: boolean;
	note: string | null;
}

export interface SettingsGroup {
	group: string;
	items: SettingItem[];
}

export interface SettingsOut {
	groups: SettingsGroup[];
}

export interface HealthOut {
	status: 'ok';
	state_db: { path: string; tracks: number; playlists: number; pairings: number };
	cloud: { lock_holder: { holder?: string; expires_at?: string } | null };
	syncthing: null | { peers_connected: number; folder_state: string };
	bind_host: string;
	version: string;
}

export class ConflictError extends Error {
	constructor(public current: Track, public etag: string) {
		super('If-Match mismatch');
	}
}

export interface PlayItGoal {
	duration_min: number;
	peak_at_min?: number | null;
	floor_energy?: number;
	ceiling_energy?: number;
}

export interface PlayItStep {
	position: number;
	stable_id: string;
	title: string | null;
	artist: string | null;
	bpm: number | null;
	key_camelot: string | null;
	energy: number | null;
	transition_hint: string;
	camelot_distance: number | null;
	bpm_delta_pct: number | null;
	target_energy: number;
	actual_energy: number;
}

export interface PlayItUnmetConstraint {
	kind: string;
	position: number;
	detail: Record<string, number | string>;
}

export interface PlayItSolveOut {
	playlist_id: string;
	etag: string;
	previous_order: string[];
	proposed_order: string[];
	unchanged: boolean;
	steps: PlayItStep[];
	constraints_unmet: PlayItUnmetConstraint[];
	solve_ms: number;
}

export class PlayItError extends Error {
	constructor(public code: string, message: string, public details: unknown = null) {
		super(message);
	}
}

export interface PlaylistWriteOut {
	playlist_id: string;
	name: string;
	vendor: string;
	vendor_pl_id: string;
	items: string[];
	track_count: number;
	created_at: string;
	updated_at: string;
}

export class PlaylistConflictError extends Error {
	constructor(public current: PlaylistWriteOut, public etag: string) {
		super('If-Match mismatch');
	}
}

export async function listTracks(params: Record<string, string | number | undefined | null> = {}): Promise<TracksPage> {
	// The client drops undefined and null query keys for us; the empty string is
	// ours to drop, and dropping it here keeps `?q=` off the wire as before.
	const query = Object.fromEntries(
		Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== '')
	) as TrackListQuery;
	const page = await unwrap(api.GET('/api/v1/tracks', { params: { query } }));
	return page as unknown as TracksPage;
}

export async function getTrack(stable_id: string): Promise<{ track: Track; etag: string }> {
	const { data, response } = await api.GET('/api/v1/tracks/{stable_id}', {
		params: { path: { stable_id } }
	});
	return { track: data as unknown as Track, etag: response.headers.get('etag') ?? '' };
}

export async function patchTrack(
	stable_id: string,
	etag: string,
	patch: { rating?: number; tags_add?: string[]; tags_remove?: string[]; notes?: string }
): Promise<{ track: Track; etag: string }> {
	let data: unknown;
	let response: Response;
	try {
		({ data, response } = await api.PATCH('/api/v1/tracks/{stable_id}', {
			params: { path: { stable_id }, header: { 'If-Match': etag } },
			body: patch
		}));
	} catch (error) {
		// The 409 body is the CAS envelope {current, etag}, not the {"detail":
		// {...}} one ApiError decodes, so read the parsed body off the error.
		if (error instanceof ApiError && error.status === 409) {
			const body = error.body as { current: Track; etag: string };
			throw new ConflictError(body.current, body.etag);
		}
		if (error instanceof ApiError) throw new Error(`PATCH failed: ${error.status}`);
		throw error;
	}
	return { track: data as unknown as Track, etag: response.headers.get('etag') ?? '' };
}

export async function listPlaylists(): Promise<PlaylistSummary[]> {
	return (await unwrap(api.GET('/api/v1/playlists'))) as unknown as PlaylistSummary[];
}

export async function getPlaylist(id: string): Promise<PlaylistDetail> {
	const detail = await unwrap(
		api.GET('/api/v1/playlists/{playlist_id}', { params: { path: { playlist_id: id } } })
	);
	return detail as unknown as PlaylistDetail;
}

/**
 * Solve a PLAY IT ordering for a playlist (pure preview, no writes).
 * `etag` in the result is the playlist's current If-Match value, valid to
 * pass straight to `replacePlaylistTracks` -- no extra fetch needed.
 */
export async function solvePlayIt(playlistId: string, goal: PlayItGoal): Promise<PlayItSolveOut> {
	try {
		const solved = await unwrap(
			api.POST('/api/v1/play-it/{playlist_id}/solve', {
				params: { path: { playlist_id: playlistId } },
				// The goal's optional fields carry server-side defaults, which the
				// generated request type spells as required.
				body: goal as paths['/api/v1/play-it/{playlist_id}/solve']['post']['requestBody']['content']['application/json']
			})
		);
		return solved as unknown as PlayItSolveOut;
	} catch (error) {
		if (error instanceof ApiError) {
			// The solve route answers with a TOP-LEVEL {error, message, details}
			// (ErrorBody), so the operator-facing code and message come off the
			// parsed body rather than ApiError's {"detail": {...}} decoding.
			const body = (error.body ?? {}) as { error?: string; message?: string; details?: unknown };
			throw new PlayItError(
				body.error ?? 'unknown',
				body.message ?? `solve failed: ${error.status}`,
				body.details
			);
		}
		throw error;
	}
}

/**
 * Apply (or undo, by passing the pre-apply order back) a PLAY IT result:
 * the single membership-replace primitive from the playlists-write
 * contract (LANE playlists-router). Throws `PlaylistConflictError` on a
 * stale etag (409) so the caller can prompt a re-solve.
 */
export async function replacePlaylistTracks(
	playlistId: string,
	stableIds: string[],
	etag: string
): Promise<{ playlist: PlaylistWriteOut; etag: string }> {
	let data: unknown;
	let response: Response;
	try {
		({ data, response } = await api.PUT('/api/v1/playlists/{playlist_id}/tracks', {
			params: { path: { playlist_id: playlistId }, header: { 'If-Match': etag } },
			body: { stable_ids: stableIds }
		}));
	} catch (error) {
		if (error instanceof ApiError && error.status === 409) {
			const body = error.body as { current: PlaylistWriteOut; etag: string };
			throw new PlaylistConflictError(body.current, body.etag);
		}
		if (error instanceof ApiError) throw new Error(`apply reorder failed: ${error.status}`);
		throw error;
	}
	return {
		playlist: data as unknown as PlaylistWriteOut,
		etag: response.headers.get('etag') ?? ''
	};
}

export async function listPairings(source?: string): Promise<Pairing[]> {
	// `source || undefined` keeps the empty string off the wire, matching the
	// old `source ? '?source=...' : ''`.
	const pairings = await unwrap(
		api.GET('/api/v1/pairings', { params: { query: { source: source || undefined } } })
	);
	return pairings as unknown as Pairing[];
}

export async function createPairing(body: {
	from_stable_id: string;
	to_stable_id: string;
	direction?: '->' | '<->';
	source?: 'manual' | 'learned' | 'ai';
	notes?: string;
}): Promise<Pairing> {
	try {
		// `direction` and `source` carry server-side defaults, which the generated
		// request type spells as required.
		const created = await unwrap(
			api.POST('/api/v1/pairings', {
				body: body as paths['/api/v1/pairings']['post']['requestBody']['content']['application/json']
			})
		);
		return created as unknown as Pairing;
	} catch (error) {
		if (error instanceof ApiError) throw new Error(`create pairing failed: ${error.status}`);
		throw error;
	}
}

export async function deletePairing(pairing_id: string, etag: string): Promise<void> {
	// 204, so no unwrap. The status is still asserted: the old code accepted
	// exactly 204, and a 2xx that is not 204 never reaches the client's
	// always-throw middleware.
	try {
		const { response } = await api.DELETE('/api/v1/pairings/{pairing_id}', {
			params: { path: { pairing_id }, header: { 'If-Match': etag } }
		});
		if (response.status !== 204) throw new Error(`delete failed: ${response.status}`);
	} catch (error) {
		if (error instanceof ApiError) throw new Error(`delete failed: ${error.status}`);
		throw error;
	}
}

export async function getQueue(kind: string): Promise<QueueOut> {
	const queue = await unwrap(api.GET('/api/v1/queues/{kind}', { params: { path: { kind } } }));
	return queue as unknown as QueueOut;
}

export async function getSettings(): Promise<SettingsOut> {
	const settings = await unwrap(api.GET('/api/v1/settings'));
	return settings as unknown as SettingsOut;
}

export interface SmartlistOut {
	id: string;
	name: string;
	rule: RuleAst;
	rule_summary: string;
	order_by: string;
	referenced_fields: string[];
	rule_schema_version: number;
	last_evaluated_at: string | null;
	created_at: string;
	modified_at: string;
}

export class SmartlistApiError extends Error {
	constructor(public status: number, message: string) {
		super(message);
	}
}

export class SmartlistConflictError extends Error {
	constructor(public current: SmartlistOut, public etag: string) {
		super('Smartlist If-Match mismatch');
	}
}

export interface SmartlistTrackOut {
	stable_id: string;
	title: string | null;
	artist: string | null;
	key: string | null;
	bpm: number | null;
	rating: number | null;
	genre: string | null;
}

/** Backend contract: `apps/webui/server` route landing on `af--gating-wave`
 * (GET /api/v1/smartlists + /{id}/tracks). No single-smartlist GET is
 * documented yet, so the edit route filters the list client-side. */
export async function listSmartlists(): Promise<SmartlistOut[]> {
	try {
		const rows = await unwrap(api.GET('/api/v1/smartlists'));
		return rows as unknown as SmartlistOut[];
	} catch (error) {
		if (error instanceof ApiError) throw new Error(`GET smartlists failed: ${error.status}`);
		throw error;
	}
}

function requiredSmartlistEtag(response: Response): string {
	const etag = response.headers.get('etag');
	if (etag === null || etag.length === 0) {
		throw new Error('smartlist response is missing ETag');
	}
	return etag;
}

export async function getSmartlist(
	id: string
): Promise<{ smartlist: SmartlistOut; etag: string }> {
	let data: unknown;
	let response: Response;
	try {
		({ data, response } = await api.GET('/api/v1/smartlists/{smartlist_id}', {
			params: { path: { smartlist_id: id } }
		}));
	} catch (error) {
		if (error instanceof ApiError) {
			throw new SmartlistApiError(error.status, `GET smartlist failed: ${error.status}`);
		}
		throw error;
	}
	const etag = requiredSmartlistEtag(response);
	return { smartlist: data as unknown as SmartlistOut, etag };
}

export async function getSmartlistTracks(id: string): Promise<SmartlistTrackOut[]> {
	try {
		const body = await unwrap(
			api.GET('/api/v1/smartlists/{smartlist_id}/tracks', {
				params: { path: { smartlist_id: id } }
			})
		);
		return body.tracks;
	} catch (error) {
		if (error instanceof ApiError) throw new Error(`GET smartlist tracks failed: ${error.status}`);
		throw error;
	}
}

/** Replace a smartlist rule through the same HTTP apply path as the editor.
 * The returned object is server-persisted readback, never an optimistic copy. */
export async function updateSmartlist(
	id: string,
	body: { rule: RuleAst; order_by?: string },
	etag: string
): Promise<{ smartlist: SmartlistOut; etag: string }> {
	let data: unknown;
	let response: Response;
	try {
		({ data, response } = await api.PUT('/api/v1/smartlists/{smartlist_id}', {
			params: { path: { smartlist_id: id }, header: { 'If-Match': etag } },
			// `RuleAst` is a discriminated union here and an open record in the
			// schema, so the two have no structural overlap to assert across.
			body: body as unknown as paths['/api/v1/smartlists/{smartlist_id}']['put']['requestBody']['content']['application/json']
		}));
	} catch (error) {
		if (error instanceof ApiError && error.status === 409) {
			// The conflict envelope is top-level {current, etag}; the header and
			// the body must agree before the caller is handed a revision to retry
			// against, so a half-updated response can never drive a silent clobber.
			const responseEtag = requiredSmartlistEtag(error.response);
			const payload = error.body as { current: SmartlistOut; etag: string };
			if (payload.etag !== responseEtag) {
				throw new Error('smartlist conflict response ETag does not match its body');
			}
			throw new SmartlistConflictError(payload.current, responseEtag);
		}
		if (error instanceof ApiError) {
			const payload = (error.body ?? {}) as { detail?: { message?: string } | string };
			const detail = typeof payload.detail === 'object' ? payload.detail?.message : payload.detail;
			throw new SmartlistApiError(error.status, detail ?? `PUT smartlist failed: ${error.status}`);
		}
		throw error;
	}
	const nextEtag = requiredSmartlistEtag(response);
	return { smartlist: data as unknown as SmartlistOut, etag: nextEtag };
}

export async function getHealth(): Promise<{ health: HealthOut; bindWarning: string | null }> {
	const { data, response } = await api.GET('/api/v1/health');
	return {
		health: data as unknown as HealthOut,
		bindWarning: response.headers.get('x-bind-warning')
	};
}
