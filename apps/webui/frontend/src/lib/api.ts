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
 * The response types are now ALIASES of the generated schemas, not hand-written
 * copies, so a call site sees exactly what the daemon documents. The drifts the
 * old hand-written block hid are therefore now visible at every call site, which
 * is the point: `TracksPage.items` is `TrackListItemOut` (seven more fields),
 * `PlaylistDetail` carries `tracks` and NO `updated_at`, `PlaylistSummary` has
 * `available_count`, `HealthOut.status` is an open string, `HealthOut.cloud
 * .lock_holder` is an open record, and `SmartlistOut.rule` is an open record
 * rather than a `RuleAst`. There is no client-boundary assertion left here: if a
 * shape stops matching, the compiler says so instead of a cast hiding it.
 */
import type { components, paths } from './api-types';
import { ApiError, api, requireBody, unwrap } from './api/client';
import type { RuleAst } from './smartlists/rule-form';

export { API_BASE } from './api/client';
export { api, unwrap, ApiError } from './api/client';

/** The documented `/api/v1/tracks` filter set. `listTracks` keeps its open
 * `Record` signature (call sites pass filter bags straight through), so the
 * bag is asserted onto this at the one point it reaches the client. */
type TrackListQuery = NonNullable<paths['/api/v1/tracks']['get']['parameters']['query']>;

/** One row of `/api/v1/tracks/{stable_id}`. NOT the row type of `TracksPage`:
 * the listing returns the wider `TrackListItemOut` (file_exists,
 * has_rb_mapping, preview_b64, preview_max, quality, stems, vocals on top of
 * these), which is why `TracksPage` aliases the schema instead of being spelled
 * `{ items: Track[] }` here. Reach those seven through `TracksPage['items']`. */
export type Track = components['schemas']['TrackOut'];
export type TracksPage = components['schemas']['TracksPage'];
export type PlaylistSummary = components['schemas']['PlaylistSummary'];
/** No `updated_at`: the server's playlist detail never carried one. The old
 * hand-written type inherited a non-optional `updated_at: string` by Omit-ing
 * `PlaylistSummary`, so every reader was promised a string that would have
 * arrived undefined. */
export type PlaylistDetail = components['schemas']['PlaylistDetail'];
export type Pairing = components['schemas']['PairingOut'];
export type QueueOut = components['schemas']['QueueOut'];
export type SettingItem = components['schemas']['SettingItem'];
export type SettingsOut = components['schemas']['SettingsOut'];
/** The daemon's schema is named EngineHealthOut; the frontend name is kept so
 * no call site moves. `status` is an open string there, not the literal 'ok'. */
export type HealthOut = components['schemas']['EngineHealthOut'];
type PairingCreateBody = Omit<components['schemas']['PairingCreate'], 'direction' | 'source'> &
	Partial<Pick<components['schemas']['PairingCreate'], 'direction' | 'source'>>;
export type PerformanceFeedbackMark = components['schemas']['PerformanceFeedbackMarkIn'];
export type PerformanceFeedbackSummary = components['schemas']['PerformanceFeedbackMarksOut'];

export class ConflictError extends Error {
	constructor(public current: Track, public etag: string) {
		super('If-Match mismatch');
	}
}

/** Engine-owned user judgements, retained independently of the browser origin. */
export async function getPerformanceFeedback(): Promise<PerformanceFeedbackSummary> {
	return requireBody(await api.GET('/api/v1/feedback/performance-marks')).data;
}

export async function createPerformanceFeedback(
	mark: PerformanceFeedbackMark
): Promise<PerformanceFeedbackSummary> {
	return requireBody(
		await api.POST('/api/v1/feedback/performance-marks', { body: mark })
	).data;
}

export interface PlayItGoal {
	duration_min: number;
	peak_at_min?: number | null;
	floor_energy?: number;
	ceiling_energy?: number;
}

export type PlayItSolveOut = components['schemas']['PlayItSolveOut'];

export class PlayItError extends Error {
	constructor(public code: string, message: string, public details: unknown = null) {
		super(message);
	}
}

export type PlaylistWriteOut = components['schemas']['PlaylistWriteOut'];

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
	return unwrap(api.GET('/api/v1/tracks', { params: { query } }));
}

export async function getTrack(stable_id: string): Promise<{ track: Track; etag: string }> {
	const { data, response } = requireBody(
		await api.GET('/api/v1/tracks/{stable_id}', { params: { path: { stable_id } } })
	);
	return { track: data, etag: response.headers.get('etag') ?? '' };
}

export async function patchTrack(
	stable_id: string,
	etag: string,
	patch: { rating?: number; tags_add?: string[]; tags_remove?: string[]; notes?: string }
): Promise<{ track: Track; etag: string }> {
	let call: { data?: Track; response: Response };
	try {
		call = await api.PATCH('/api/v1/tracks/{stable_id}', {
			params: { path: { stable_id }, header: { 'If-Match': etag } },
			body: patch
		});
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
	const { data, response } = requireBody(call);
	return { track: data, etag: response.headers.get('etag') ?? '' };
}

export async function listPlaylists(): Promise<PlaylistSummary[]> {
	return unwrap(api.GET('/api/v1/playlists'));
}

export async function getPlaylist(id: string): Promise<PlaylistDetail> {
	return unwrap(
		api.GET('/api/v1/playlists/{playlist_id}', { params: { path: { playlist_id: id } } })
	);
}

/**
 * Solve a PLAY IT ordering for a playlist (pure preview, no writes).
 * `etag` in the result is the playlist's current If-Match value, valid to
 * pass straight to `replacePlaylistTracks` -- no extra fetch needed.
 */
export async function solvePlayIt(playlistId: string, goal: PlayItGoal): Promise<PlayItSolveOut> {
	try {
		return await unwrap(
			api.POST('/api/v1/play-it/{playlist_id}/solve', {
				params: { path: { playlist_id: playlistId } },
				// The goal's optional fields carry server-side defaults, which the
				// generated request type spells as required.
				body: goal as components['schemas']['PlayItGoalIn']
			})
		);
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
	let call: { data?: PlaylistWriteOut; response: Response };
	try {
		call = await api.PUT('/api/v1/playlists/{playlist_id}/tracks', {
			params: { path: { playlist_id: playlistId }, header: { 'If-Match': etag } },
			body: { stable_ids: stableIds }
		});
	} catch (error) {
		if (error instanceof ApiError && error.status === 409) {
			const body = error.body as { current: PlaylistWriteOut; etag: string };
			throw new PlaylistConflictError(body.current, body.etag);
		}
		if (error instanceof ApiError) throw new Error(`apply reorder failed: ${error.status}`);
		throw error;
	}
	const { data, response } = requireBody(call);
	return { playlist: data, etag: response.headers.get('etag') ?? '' };
}

export async function listPairings(source?: string): Promise<Pairing[]> {
	// `source || null` keeps the empty string off the wire, matching the old
	// `source ? '?source=...' : ''`. null rather than undefined because the
	// generated query type is `source?: string | null` and
	// exactOptionalPropertyTypes takes an optional key at its word; the two
	// are wire-identical, openapi-fetch's serializer skips both.
	return unwrap(
		api.GET('/api/v1/pairings', { params: { query: { source: source || null } } })
	);
}

/** Every pairing touching `stableId`, either direction. The route only
 * filters by ONE side per call (`from_stable_id` XOR `to_stable_id`), so this
 * issues both and merges by `pairing_id` - a pairing is directional data
 * (`direction`), but "is this track paired with anything" has to look both
 * ways. Used to default-show the paired track in the recommended bar (pin
 * 72ac80073f92 / issue #878). */
export async function listPairingsFor(stableId: string): Promise<Pairing[]> {
	const [asFrom, asTo] = await Promise.all([
		unwrap(
			api.GET('/api/v1/pairings', {
				params: { query: { from_stable_id: stableId, to_stable_id: null, source: null } }
			})
		),
		unwrap(
			api.GET('/api/v1/pairings', {
				params: { query: { from_stable_id: null, to_stable_id: stableId, source: null } }
			})
		)
	]);
	const byId = new Map<string, Pairing>();
	for (const p of [...asFrom, ...asTo]) byId.set(p.pairing_id, p);
	return [...byId.values()];
}

export async function createPairing(body: PairingCreateBody): Promise<Pairing> {
	try {
		// `direction` and `source` carry server-side defaults, which the generated
		// request type spells as required.
		return await unwrap(
			api.POST('/api/v1/pairings', {
				body: body as components['schemas']['PairingCreate']
			})
		);
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

export type SyncSnapshot = components['schemas']['SyncSnapshotOut'];
export type Alignment = components['schemas']['AlignmentOut'];
type AlignmentCreateBody = components['schemas']['AlignmentIn'];

/** PAIR-02's durable LV1 snapshots (`/api/v1/pairings/sync-snapshots`, GET).
 * `stable_a`/`stable_b` match directionally, unlike `listAlignments` below --
 * see `lib/rb/pairing-capture.ts` for the query-both-orders caller. */
export async function listSyncSnapshots(
	stable_a: string,
	stable_b: string,
	limit: number
): Promise<SyncSnapshot[]> {
	return unwrap(
		api.GET('/api/v1/pairings/sync-snapshots', { params: { query: { stable_a, stable_b, limit } } })
	);
}

/** PAIR-02's durable LV2 alignment marks (`/api/v1/pairings/alignments`, POST). */
export async function createAlignment(body: AlignmentCreateBody): Promise<Alignment> {
	return unwrap(api.POST('/api/v1/pairings/alignments', { body }));
}

export async function getQueue(kind: string): Promise<QueueOut> {
	return unwrap(api.GET('/api/v1/queues/{kind}', { params: { path: { kind } } }));
}

export async function getSettings(): Promise<SettingsOut> {
	return unwrap(api.GET('/api/v1/settings'));
}

/** `rule` is an open record here, not a `RuleAst`: the daemon documents it as
 * one (`SmartlistSummary.rule`) and the editor narrows it with `astToForm`. */
export type SmartlistOut = components['schemas']['SmartlistSummary'];

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

export type SmartlistTrackOut = components['schemas']['TrackRowOut'];

/** Backend contract: `apps/webui/server` route landing on `af--gating-wave`
 * (GET /api/v1/smartlists + /{id}/tracks). No single-smartlist GET is
 * documented yet, so the edit route filters the list client-side. */
export async function listSmartlists(): Promise<SmartlistOut[]> {
	try {
		return await unwrap(api.GET('/api/v1/smartlists'));
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
	let call: { data?: SmartlistOut; response: Response };
	try {
		call = await api.GET('/api/v1/smartlists/{smartlist_id}', {
			params: { path: { smartlist_id: id } }
		});
	} catch (error) {
		if (error instanceof ApiError) {
			throw new SmartlistApiError(error.status, `GET smartlist failed: ${error.status}`);
		}
		throw error;
	}
	const { data, response } = requireBody(call);
	const etag = requiredSmartlistEtag(response);
	return { smartlist: data, etag };
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
	let call: { data?: SmartlistOut; response: Response };
	try {
		call = await api.PUT('/api/v1/smartlists/{smartlist_id}', {
			params: { path: { smartlist_id: id }, header: { 'If-Match': etag } },
			// A spread, not an assertion: `RuleAst` is a closed union of
			// interfaces and the schema's `rule` is an open record, so the AST
			// has to widen into one rather than be asserted onto it. `?? null`
			// for the same exactOptionalPropertyTypes reason as listPairings
			// above: the generated type is `order_by?: string | null`.
			body: { rule: { ...body.rule }, order_by: body.order_by ?? null }
		});
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
	const { data, response } = requireBody(call);
	const nextEtag = requiredSmartlistEtag(response);
	return { smartlist: data, etag: nextEtag };
}

export async function getHealth(): Promise<{ health: HealthOut; bindWarning: string | null }> {
	const { data, response } = requireBody(await api.GET('/api/v1/health'));
	return { health: data, bindWarning: response.headers.get('x-bind-warning') };
}

/**
 * `AbortSignal.timeout` equivalent built from `AbortController` + `setTimeout`
 * (Sol review thread 3966717870 on PR #1560). `AbortSignal.timeout` needs
 * Safari 16; the desktop app's `minimumSystemVersion` in
 * `apps/desktop/src-tauri/tauri.conf.json` is "11.0", and macOS 11 Big Sur
 * tops out at Safari/WebKit 15.x - so on a supported install the static
 * method is simply absent and calling it throws before any fetch is issued.
 * No feature-detect-and-fall-back: a second, untested code path is worse
 * than one path that works everywhere.
 *
 * The timer is cleared by the caller (`finally`) on both the success and
 * throw paths, so a settled probe never leaves a pending timer behind.
 * Exported because `BrowserPanel.svelte`'s `_pingFrontend` needs the exact
 * same signal for a plain `fetch()` call outside the OpenAPI client.
 */
export function timeoutSignal(timeoutMs: number): { signal: AbortSignal; clear: () => void } {
	const controller = new AbortController();
	const timer = setTimeout(
		() => controller.abort(new DOMException('The operation timed out.', 'TimeoutError')),
		timeoutMs
	);
	return { signal: controller.signal, clear: () => clearTimeout(timer) };
}

/**
 * Liveness-only health read for the Backend status dot (pin a66ee132a14e).
 *
 * Separate from `getHealth` on purpose: this one must not hang, so it carries
 * its own abort timeout and bypasses any cache, and it wants nothing from the
 * body. It lives here rather than in the component for the same fan-in reason
 * documented below - `src/lib/api/client.ts` sits at its recorded floor, so
 * new direct importers of it are not free.
 *
 * Throws on any non-2xx or on timeout; the caller turns that into a red dot
 * carrying the reason.
 */
export async function pingHealth(timeoutMs: number): Promise<void> {
	const { signal, clear } = timeoutSignal(timeoutMs);
	try {
		requireBody(
			await api.GET('/api/v1/health', {
				cache: 'no-store',
				signal
			})
		);
	} finally {
		clear();
	}
}

/** PREFLIGHT-01's boot gate (issue #771) reads `GET /api/v1/preflight`.
 *
 * It lives HERE, beside `getHealth`, rather than in its own
 * `src/lib/preflight/` transport module, for one measured reason: every new
 * direct importer of `src/lib/api/client.ts` moves `frontend.max_fan_in`,
 * which sits at its recorded floor with zero headroom (ops/quality/README.md
 * -- allowances only shrink). This module already depends on the client, so
 * routing the call through it adds no edge, and preflight is a health-family
 * read anyway: same daemon, same "is this thing ready" question.
 *
 * One call serves both "Re-check" and "Re-request permissions": the server's
 * audio-access check performs the real gated read every time it runs, so a
 * second GET after granting an OS permission is both at once. There is no
 * separate mutating endpoint to keep in sync with this one -- see
 * `apps/webui/server/routes/preflight.py`.
 *
 * The verdict is READ, never recomputed: callers take `PreflightOut.status`
 * as given rather than deriving pass/fail from the check list, so the server
 * stays the one source of truth (this issue's own parity clause).
 */
export type PreflightResult = components['schemas']['PreflightOut'];
export type PreflightCheck = components['schemas']['PreflightCheckOut'];

export async function getPreflight(): Promise<PreflightResult> {
	return unwrap(api.GET('/api/v1/preflight'));
}
