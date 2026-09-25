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
import { ApiError, api, API_BASE, apiErrorFrom, requireBody, unwrap } from './api/client';
import { subscribeKind, subscribeResync } from './api/events-bus';
import { BOOT_COALESCE_TTL_MS, requestCoalescer } from './api/request-coalescer';
import { rememberOptionalResources } from './rb/optional-resource-availability';
import { withSessionRating } from './rb/stick-session-edits';
import { isUsbTrackId, refuseStickWrite, trackApiPath } from './rb/track-source';

export { API_BASE } from './api/client';
export {
	api,
	unwrap,
	ApiError,
	RbApiError,
	readApiErrorCode,
	readApiErrorStatus
} from './api/client';

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
	peak_pins?: string[];
	opener_pins?: string[];
	closer_pin?: string | null;
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

function _rememberTrackOptionalResources(track: Track): void {
	const partial: {
		lyrics?: boolean;
		autoCues?: boolean;
		stems?: boolean;
		artwork?: boolean | null;
	} = {};
	if (typeof track.lyrics_available === 'boolean') {
		partial.lyrics = track.lyrics_available;
	}
	if (typeof track.auto_cues_available === 'boolean') {
		partial.autoCues = track.auto_cues_available;
	}
	if (typeof track.stems_available === 'boolean') {
		partial.stems = track.stems_available;
	}
	if ('artwork_available' in track) {
		partial.artwork = track.artwork_available;
	}
	if (Object.keys(partial).length > 0) {
		rememberOptionalResources(track.stable_id, partial);
	}
}

export async function getTrack(stable_id: string): Promise<{ track: Track; etag: string }> {
	const { data, response } = isUsbTrackId(stable_id)
		? await _getUsbTrack(stable_id)
		: requireBody(await api.GET('/api/v1/tracks/{stable_id}', { params: { path: { stable_id } } }));
	_rememberTrackOptionalResources(data);
	return { track: data, etag: response.headers.get('etag') ?? '' };
}

/** Spec 4b: a stick track's TrackOut comes from GET /api/v1/usb/tracks/{id}
 * (the same response model). A raw fetch only because that route is not in
 * the generated schema yet; failures still throw ApiError with the route's
 * detail.code (USB_STICK_NOT_MOUNTED and friends), same as a typed call.
 * Carries this session's rating for the track (decision 2). */
async function _getUsbTrack(stable_id: string): Promise<{ data: Track; response: Response }> {
	const response = await globalThis.fetch(`${API_BASE}${trackApiPath(stable_id)}`, {
		headers: { Accept: 'application/json' }
	});
	if (!response.ok) throw await apiErrorFrom(response);
	const { data } = requireBody({ data: (await response.json()) as Track | null, response });
	return { data: withSessionRating(stable_id, data), response };
}

export type TempoPrefPatch = components['schemas']['TempoPrefPatch'];

export async function patchTrack(
	stable_id: string,
	etag: string,
	patch: {
		rating?: number;
		tags_add?: string[];
		tags_remove?: string[];
		notes?: string;
		tempo_pref?: TempoPrefPatch | null;
	}
): Promise<{ track: Track; etag: string }> {
	// Spec decision 2: nothing is ever written for a stick track.
	refuseStickWrite(stable_id, 'track edit');
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
	_rememberTrackOptionalResources(data);
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

/** The coalescer key for the health body read. One string, one endpoint. */
const HEALTH_KEY = 'GET /api/v1/health';

/**
 * A cached health read must not outlive the question it answered: a track
 * import (or any change to `state_db.tracks`) inside the coalescer's TTL
 * would otherwise be invisible to the next `getHealth()` caller for up to
 * BOOT_COALESCE_TTL_MS, which is exactly the window BrowserPanel reads
 * `allTracksCount` from to decide whether the boot pane has anything to
 * show (PR #1656 review thread on this line). A resync (seq gap) also
 * invalidates: a gap means something was missed and health may be one of
 * the things that changed, so serving the pre-gap body is the same bug.
 *
 * `subscribeResync` alone used to miss the bus's very first successful
 * connection, which needed a second, explicit `subscribeConnectionState`
 * invalidation here to cover (PR #1656 review round 4). That gap is now
 * closed at the source: events-bus.ts fires a resync (reason
 * `'initial-connect'`) on the first open too, not just a reconnect, so this
 * one subscription now covers both cases and the extra listener was removed.
 *
 * `forceInFlight: false` for that one reason only (review round 5): nothing
 * about SERVER state changed just because the bus connected for the first
 * time, unlike a real 'tracks' change or a seq gap, so an entry already in
 * flight is not stale against it and does not need to be force-reissued --
 * doing so anyway bought no correctness and cost an intermittent fifth
 * request racing the one already in flight (`request-coalescer.test.mjs`
 * covers this directly).
 */
subscribeKind('tracks', () => requestCoalescer.invalidate(HEALTH_KEY));
subscribeResync((reason) =>
	requestCoalescer.invalidate(HEALTH_KEY, { forceInFlight: reason !== 'initial-connect' })
);

/**
 * How long the coalesced health fetch may run before it is abandoned.
 *
 * PR #1656 review thread on `request-coalescer.ts:158`: the coalescer joins
 * an in-flight request for as long as it stays in flight, with no bound, so
 * a health fetch that never settles (a stalled connection) would otherwise
 * wedge every caller inside the TTL forever, and the coalescer has no clock
 * of its own to recover from that -- it only drops an entry on REJECTION.
 * Bounding the fetch itself, the same way `pingHealth`'s `CONN_PING_TIMEOUT_MS`
 * already does for the liveness dot, turns a stall into an ordinary rejection
 * the coalescer already handles correctly (see "a rejected call is dropped"
 * in request-coalescer.test.mjs). The largest real fetchWall this module's
 * docstring ever measured, for an 8000-row library, was ~3579ms; this sits
 * roughly 3x above that, comfortably clear of realistic load while still
 * bounding how long a genuine stall can hold the boot pane blank.
 */
const HEALTH_FETCH_TIMEOUT_MS = 10_000;

function _fetchHealthBody() {
	const { signal, clear } = timeoutSignal(HEALTH_FETCH_TIMEOUT_MS);
	return api.GET('/api/v1/health', { signal }).finally(clear);
}

/**
 * The daemon's health body, shared with any other caller asking inside the
 * boot window (see `src/lib/api/request-coalescer.ts` for the measurement
 * that motivated this and the TTL derivation).
 *
 * Pass `fresh: true` when the caller is reacting to a CHANGE and needs the
 * value it is refreshing to, rather than the value the page already has.
 * `_refreshLibraryRowsOnce` in BrowserPanel is the case: it runs off library
 * invalidation events, so serving it a body from before the change it is
 * reacting to would paint a stale track count and leave it there until the
 * next event. Correctness beats one request.
 */
export async function getHealth(
	options: { fresh?: boolean } = {}
): Promise<{ health: HealthOut; bindWarning: string | null }> {
	// NOT shared with the capability probe, deliberately: that probe reads the
	// raw bytes to tell a legacy daemon from an engine one, and carries its own
	// memoization with its own rules. A 2s TTL underneath it would change what
	// "the daemon is legacy" means, since a daemon whose identity changed inside
	// the window would keep reporting the identity it had at the start of it.
	// Joining it was tried and reverted; daemon-capabilities.test.mjs refuses it.
	const call =
		options.fresh === true
			? _fetchHealthBody()
			: requestCoalescer.share(HEALTH_KEY, BOOT_COALESCE_TTL_MS, _fetchHealthBody);
	// A joined caller reads HEADERS off a shared Response whose body stream
	// the client has already parsed into `data`. Headers are re-readable;
	// the stream is not, and nothing here touches it.
	const { data, response } = requireBody(await call);
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

// ------------------------------------------------------------------ karaoke words (LYR-03 / #2079)
// lyrics-cache.svelte.ts is the sole caller of getTrackLyricsWords.

export type LyricWord = components['schemas']['KaraokeWordOut'];
export type LyricLine = components['schemas']['KaraokeLineOut'];
export type LyricVerdict = components['schemas']['CoverageVerdictOut'];
export type LyricTrack = components['schemas']['KaraokeTrackOut'];
export type LyricSummary = components['schemas']['KaraokeSummaryOut'];
export type LyricsConfig = components['schemas']['LyricsConfigOut'];
export type LyricJob = components['schemas']['LyricJobOut'];
export type LyricPurgeResult = components['schemas']['LyricsPurgeOut'];

export type LyricVerdictValue = 'vocal' | 'sparse' | 'no-lyrics' | 'unknown';
export type LyricWitness = NonNullable<LyricWord['witness']>;
export type LyricJobKind = 'analyze' | 'lyricsync' | 'stems';
export type LyricJobStatus = 'queued' | 'done' | 'failed';

export interface ListLyricVerdictsParams {
	limit?: number;
	offset?: number;
	verdict?: string;
	order?: 'suspect' | 'coverage' | 'recent';
}

/** Karaoke words for one track. Returns null on 404 (no verdict, tombstone, or artifact). */
export async function getTrackLyricsWords(
	stable_id: string,
	opts: { includeLines?: boolean } = {}
): Promise<LyricTrack | null> {
	// Spec 4b: a stick track has no lyrics route, so it is the no-lyrics state.
	if (isUsbTrackId(stable_id)) return null;
	try {
		return await unwrap(
			api.GET('/api/v1/tracks/{stable_id}/lyrics/words', {
				params: {
					path: { stable_id },
					...(opts.includeLines ? { query: { include: 'lines' as const } } : {})
				}
			})
		);
	} catch (error) {
		if (
			typeof error === 'object' &&
			error !== null &&
			(error as { status?: number }).status === 404
		) {
			return null;
		}
		throw error;
	}
}

export async function putLyricOverride(
	stable_id: string,
	override: LyricVerdictValue | null,
	note?: string
): Promise<LyricVerdict> {
	// Spec decision 2: nothing is ever written for a stick track.
	refuseStickWrite(stable_id, 'lyric override');
	return unwrap(
		api.PUT('/api/v1/tracks/{stable_id}/lyrics/override', {
			params: { path: { stable_id } },
			body: { override, note: note ?? null }
		})
	);
}

export async function listLyricVerdicts(
	params: ListLyricVerdictsParams = {}
): Promise<LyricVerdict[]> {
	return unwrap(api.GET('/api/v1/lyrics', { params: { query: params } }));
}

export async function getLyricSummary(): Promise<LyricSummary> {
	return unwrap(api.GET('/api/v1/lyrics/summary'));
}

export async function getLyricsConfig(): Promise<LyricsConfig> {
	return unwrap(api.GET('/api/v1/lyrics/config'));
}

export async function putLyricsConfig(source_order: string[]): Promise<LyricsConfig> {
	return unwrap(
		api.PUT('/api/v1/lyrics/config', {
			body: { source_order }
		})
	);
}

export async function listLyricJobs(): Promise<LyricJob[]> {
	return unwrap(api.GET('/api/v1/lyrics/jobs'));
}

export async function postLyricJob(
	kind: LyricJobKind,
	stable_ids: string[],
	note?: string
): Promise<LyricJob> {
	return unwrap(
		api.POST('/api/v1/lyrics/jobs', {
			body: { kind, stable_ids, note: note ?? null }
		})
	);
}

export async function postLyricsPurge(
	source_prefix: string,
	dry_run = false
): Promise<LyricPurgeResult> {
	return unwrap(
		api.POST('/api/v1/lyrics/purge', {
			body: { source_prefix, dry_run }
		})
	);
}

export async function getLyricsKpiLedger(): Promise<Record<string, unknown>> {
	return unwrap(api.GET('/api/v1/bench/lyrics-kpi'));
}

export interface RbDjayPlaylistPlanResult {
	plan_path: string;
	patch_csv: string;
	diff_md: string;
	op_total: number;
	summary: {
		create: number;
		update: number;
		noop: number;
		djay_only_playlists: number;
		membership_adds: number;
		membership_removes: number;
	};
}

export interface RbDjayPlaylistDiffResult {
	computed: boolean;
	reason?: string;
	diff: PlaylistDetail['diff'];
	summary?: Record<string, unknown>;
}

function _requireRecord(raw: unknown, label: string): Record<string, unknown> {
	if (typeof raw !== 'object' || raw === null) {
		throw new Error(`${label}: expected an object response`);
	}
	return raw as Record<string, unknown>;
}

function _requireString(raw: unknown, label: string): string {
	if (typeof raw !== 'string') {
		throw new Error(`${label}: expected a string field`);
	}
	return raw;
}

function _requireNumber(raw: unknown, label: string): number {
	if (typeof raw !== 'number' || !Number.isFinite(raw)) {
		throw new Error(`${label}: expected a finite number field`);
	}
	return raw;
}

function _parseRbDjayPlanSummary(raw: unknown): RbDjayPlaylistPlanResult['summary'] {
	const summary = _requireRecord(raw, 'rb-djay playlist plan summary');
	return {
		create: _requireNumber(summary.create, 'rb-djay playlist plan summary.create'),
		update: _requireNumber(summary.update, 'rb-djay playlist plan summary.update'),
		noop: _requireNumber(summary.noop, 'rb-djay playlist plan summary.noop'),
		djay_only_playlists: _requireNumber(
			summary.djay_only_playlists,
			'rb-djay playlist plan summary.djay_only_playlists'
		),
		membership_adds: _requireNumber(
			summary.membership_adds,
			'rb-djay playlist plan summary.membership_adds'
		),
		membership_removes: _requireNumber(
			summary.membership_removes,
			'rb-djay playlist plan summary.membership_removes'
		)
	};
}

function _parseRbDjayPlaylistPlanResult(raw: unknown): RbDjayPlaylistPlanResult {
	const body = _requireRecord(raw, 'rb-djay playlist plan');
	return {
		plan_path: _requireString(body.plan_path, 'rb-djay playlist plan.plan_path'),
		patch_csv: _requireString(body.patch_csv, 'rb-djay playlist plan.patch_csv'),
		diff_md: _requireString(body.diff_md, 'rb-djay playlist plan.diff_md'),
		op_total: _requireNumber(body.op_total, 'rb-djay playlist plan.op_total'),
		summary: _parseRbDjayPlanSummary(body.summary)
	};
}

function _parseRbDjayPlaylistDiffResult(raw: unknown): RbDjayPlaylistDiffResult {
	const body = _requireRecord(raw, 'rb-djay playlist diff');
	const computed = body.computed;
	if (typeof computed !== 'boolean') {
		throw new Error('rb-djay playlist diff.computed: expected a boolean field');
	}
	const diff = body.diff;
	if (typeof diff !== 'object' || diff === null) {
		throw new Error('rb-djay playlist diff.diff: expected an object field');
	}
	const result: RbDjayPlaylistDiffResult = {
		computed,
		diff: diff as PlaylistDetail['diff']
	};
	if (typeof body.reason === 'string') {
		result.reason = body.reason;
	}
	if (typeof body.summary === 'object' && body.summary !== null) {
		result.summary = body.summary as Record<string, unknown>;
	}
	return result;
}

/** SYNC-03 dry-run planner: POST /api/v1/rb-djay-sync/playlists/plan */
export async function planRbDjayPlaylistSync(body: {
	matches_path?: string;
	only_playlists?: string[];
	max_ops?: number;
}): Promise<RbDjayPlaylistPlanResult> {
	const requestBody: components['schemas']['PlaylistPlanRequest'] = {
		max_ops: body.max_ops ?? 10_000,
		...(body.matches_path !== undefined ? { matches_path: body.matches_path } : {}),
		...(body.only_playlists !== undefined ? { only_playlists: body.only_playlists } : {})
	};
	return _parseRbDjayPlaylistPlanResult(
		requireBody(await api.POST('/api/v1/rb-djay-sync/playlists/plan', { body: requestBody })).data
	);
}

/** Saved playlist-plan buckets for one playlist detail page. */
export async function getPlaylistRbDjayDiff(
	playlistId: string
): Promise<RbDjayPlaylistDiffResult> {
	return _parseRbDjayPlaylistDiffResult(
		requireBody(
			await api.GET('/api/v1/playlists/{playlist_id}/rb-djay-diff', {
				params: { path: { playlist_id: playlistId } }
			})
		).data
	);
}
