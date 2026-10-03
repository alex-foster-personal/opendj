/**
 * Typed fetch helpers for the 4 NEW rb-assets endpoints
 * (COMPONENT-MAP.md section 2), plus re-exports of the existing
 * track/playlist client so /performance builders import everything
 * API-shaped from this one module.
 *
 * Base URL: mirrors src/lib/api.ts (relative '/api/...' in the browser via
 * the Vite dev proxy / prod same-origin; absolute host during SSR), with a
 * VITE_API_BASE env override shared with the hand-written API client.
 *
 * Fail-fast: every helper checks r.ok and throws RbApiError carrying the
 * backend's explicit {"detail": {code, message}} payload. No silent
 * fallbacks, no invented data.
 */

import { API_BASE } from '$lib/api';
import type { PlaylistDetail, PlaylistSummary, Track } from '$lib/api';
import type { components } from '$lib/api-types';
import { api, unwrap } from '$lib/api/client';
import { RbApiError } from './api-rb-error';
import { currentAnlzFetchGeneration } from './anlz-fetch-generation';
import { optionalResources } from './optional-resource-availability';
import type { AnlzCue, AnlzData } from './anlz-types';
import type { HotCueSlot } from './hot-cue-types';
import type { ArtworkSize, QualityRung, RbMeta, TrackQuality } from './library-types';
import type { LyricsRowSummary } from './lyrics/types';
import { anlzQuery, defaultAnlzPoints } from './runtime-policy-points';
import { stemWorkSignal } from './stem-decode-policy';

// Re-export the existing hand-written client (RECON-FRONTEND 3).
export {
	ConflictError,
	getHealth,
	getPlaylist,
	getTrack,
	listPlaylists,
	listTracks,
	patchTrack,
	pingHealth,
	timeoutSignal
} from '$lib/api';
export type {
	HealthOut,
	PlaylistDetail,
	PlaylistSummary,
	Track,
	TracksPage
} from '$lib/api';

export const RB_API_BASE: string = API_BASE;

export { RbApiError } from './api-rb-error';

export type FileAvailabilityStatus =
	| 'present'
	| 'absent'
	| 'AVAILABILITY_PENDING'
	| 'streaming'
	| 'awaiting_volume';

export type TrackLyrics = {
	stable_id: string;
	source: string;
	lines: Array<{ start_ms: number; text: string }>;
};

// ----------------------------------------------------------- _helpers

async function _throwRbApiError(r: Response): Promise<never> {
	// Backend contract: errors are explicit JSON {"detail": {code, message}}
	// (COMPONENT-MAP 2 shared plumbing). ServerErrorMiddleware emits plain
	// text for unhandled errors, so parse text defensively to preserve the
	// HTTP status rather than leaking a JSON SyntaxError to the user.
	const text = await r.text();
	let body: unknown;
	try {
		body = JSON.parse(text) as unknown;
	} catch {
		throw new RbApiError(r.status, `HTTP_${r.status}`, text.slice(0, 200));
	}

	const detail =
		typeof body === 'object' && body !== null && 'detail' in body
			? body.detail
			: undefined;
	if (
		typeof detail === 'object' &&
		detail !== null &&
		'code' in detail &&
		'message' in detail &&
		typeof detail.code === 'string' &&
		typeof detail.message === 'string'
	) {
		throw new RbApiError(r.status, detail.code, detail.message);
	}

	throw new RbApiError(r.status, `HTTP_${r.status}`, text.slice(0, 200));
}

async function _fetchJson<T>(path: string, cache?: RequestCache): Promise<T> {
	const init: RequestInit = { headers: { Accept: 'application/json' } };
	if (cache !== undefined) init.cache = cache;
	const r = await fetch(`${RB_API_BASE}${path}`, init);
	if (!r.ok) await _throwRbApiError(r);
	return (await r.json()) as T;
}

export async function fetchTrackifyLibraryRevision(): Promise<string> {
	const payload = await _fetchJson<{ revision: unknown }>('/api/v1/tracks/revision', 'no-store');
	if (typeof payload.revision !== 'string' || payload.revision === '') {
		throw new Error('Trackify: library revision response is invalid');
	}
	return payload.revision;
}

/** The shared GET-JSON path (RbApiError on non-2xx), for route-lazy modules
 * that keep their endpoint helpers out of this first-paint module. */
export { _fetchJson as fetchRbJson };

function _parseTrackLyrics(raw: unknown, stableId: string): TrackLyrics {
	if (typeof raw !== 'object' || raw === null) throw new Error('lyrics response must be an object');
	const lyrics = raw as { stable_id?: unknown; source?: unknown; lines?: unknown };
	if (lyrics.stable_id !== stableId || typeof lyrics.source !== 'string' || !lyrics.source) {
		throw new Error('lyrics response has invalid stable_id or source');
	}
	if (!Array.isArray(lyrics.lines) || lyrics.lines.length === 0) {
		throw new Error('lyrics response has no line-level lyrics');
	}
	let previousStartMs = -1;
	const lines = lyrics.lines.map((line, index) => {
		if (typeof line !== 'object' || line === null) throw new Error(`lyrics line ${index} is invalid`);
		const value = line as { start_ms?: unknown; text?: unknown };
		if (
			typeof value.start_ms !== 'number' ||
			!Number.isInteger(value.start_ms) ||
			value.start_ms < 0 ||
			// Equal stamps are valid LRC (two lines sung at once), and the
			// server's cache reader accepts them (apps/lyrics/cache.py); only a
			// line that starts BEFORE the previous one is out of order.
			value.start_ms < previousStartMs ||
			typeof value.text !== 'string' ||
			!value.text.trim()
		) {
			throw new Error(`lyrics line ${index} is invalid`);
		}
		previousStartMs = value.start_ms;
		return { start_ms: value.start_ms, text: value.text };
	});
	return { stable_id: stableId, source: lyrics.source, lines };
}

/** GET cached line-synced lyrics. A 404 is the explicit no-lyrics state. */
export async function fetchTrackLyrics(stableId: string): Promise<TrackLyrics | null> {
	if (optionalResources(stableId).lyrics === false) return null;
	try {
		return _parseTrackLyrics(
			await _fetchJson<unknown>(`/api/v1/tracks/${encodeURIComponent(stableId)}/lyrics`),
			stableId
		);
	} catch (error) {
		if (error instanceof RbApiError && error.status === 404) return null;
		throw error;
	}
}

async function _putJson<T>(path: string, body: unknown, ifMatch?: string): Promise<T> {
	const r = await fetch(`${RB_API_BASE}${path}`, {
		method: 'PUT',
		headers: {
			'Content-Type': 'application/json',
			Accept: 'application/json',
			...(ifMatch === undefined ? {} : { 'If-Match': ifMatch })
		},
		body: JSON.stringify(body)
	});
	if (!r.ok) await _throwRbApiError(r);
	return (await r.json()) as T;
}

async function _deleteRequest<T>(path: string, ifMatch?: string): Promise<T> {
	const r = await fetch(`${RB_API_BASE}${path}`, {
		method: 'DELETE',
		...(ifMatch === undefined ? {} : { headers: { 'If-Match': ifMatch } })
	});
	if (!r.ok) await _throwRbApiError(r);
	return (await r.json()) as T;
}

// --------------------------------------------- /anlz vocals (SPIKE-B1/B2)
// Shared contract point 5: GET /tracks/{sid}/anlz gains field "vocals",
// exactly one of the four states below. The UI states are MANDATORY
// (bars / analyzed-no-vocals / not-analyzed) - bars are never rendered
// from anything but real regions. 'demucs' is the SPIKE-B2 local
// detection fallback (data/state/vocal-cache) merged server-side when
// PVDI is absent; 'not_analyzed' now means NEITHER source exists.

/** One vocal region. For 'rekordbox' intensity is the max PVDI value
 * (1..4) within the run; for 'demucs' it is the worker confidence
 * mapped onto the same 1..4 ramp, so both render identically. */
export interface VocalRegion {
	start_s: number;
	end_s: number;
	intensity: number;
}

export type Vocals =
	| { status: 'rekordbox'; fps: number; regions: VocalRegion[] }
	| { status: 'no_vocals'; fps: number; regions: VocalRegion[] } // regions always []
	| { status: 'demucs'; fps: number; regions: VocalRegion[] } // local detection; [] = none found
	| { status: 'not_analyzed' };

/** AnlzData plus the contract's vocals field. types.ts is the frozen
 * build-unit contract, so the widening lives here (api-rb owns types). */
export type AnlzWithVocals = AnlzData & { vocals: Vocals };

const _vocalsMemo = new WeakMap<AnlzData, Vocals>();

/** Validate a wire ``vocals`` object (listing rows or /anlz). Throws on
 * absent/malformed: that is a contract breach, never invent regions. */
export function parseVocals(raw: unknown): Vocals {
	if (typeof raw !== 'object' || raw === null) {
		throw new Error(
			'payload has no valid "vocals" field - backend contract point 5 not met'
		);
	}
	const v = raw as { status?: unknown; fps?: unknown; regions?: unknown };
	if (v.status === 'not_analyzed') return { status: 'not_analyzed' };
	if (v.status !== 'rekordbox' && v.status !== 'no_vocals' && v.status !== 'demucs') {
		throw new Error(`anlz vocals: unknown status ${JSON.stringify(v.status)}`);
	}
	if (typeof v.fps !== 'number') throw new Error('anlz vocals: fps missing or not a number');
	if (!Array.isArray(v.regions)) throw new Error('anlz vocals: regions missing or not an array');
	const regions = v.regions.map((r: unknown, i: number): VocalRegion => {
		const rr = r as { start_s?: unknown; end_s?: unknown; intensity?: unknown };
		if (
			typeof rr.start_s !== 'number' ||
			typeof rr.end_s !== 'number' ||
			typeof rr.intensity !== 'number'
		) {
			throw new Error(`anlz vocals: region ${i} malformed (need start_s/end_s/intensity numbers)`);
		}
		return { start_s: rr.start_s, end_s: rr.end_s, intensity: rr.intensity };
	});
	if (v.status === 'no_vocals' && regions.length !== 0) {
		throw new Error('anlz vocals: status no_vocals but regions non-empty - contract violation');
	}
	if (v.status === 'rekordbox' && regions.length === 0) {
		throw new Error(
			'anlz vocals: status rekordbox with zero regions - the contract calls that no_vocals'
		);
	}
	return { status: v.status, fps: v.fps, regions };
}

/** Validated vocals of an anlz payload (memoized per payload object -
 * safe to call from rAF paint paths). Throws loudly when the field is
 * absent/malformed: that is a contract breach, NOT one of the three UI
 * states, and must never be rendered as "no vocals". */
export function vocalsOf(anlz: AnlzData): Vocals {
	const memo = _vocalsMemo.get(anlz);
	if (memo !== undefined) return memo;
	const vocals = parseVocals((anlz as AnlzData & { vocals?: unknown }).vocals);
	_vocalsMemo.set(anlz, vocals);
	return vocals;
}

// ------------------------------------- preview strips (SPIKE-A1/A2)
// Shared contract point 1: listing rows carry preview_b64 (base64 of
// 120 cols x 3 bytes [low, mid, hi], uint8 0..127, peak-downsampled from
// the ANLZ .2EX PWV6 1200-col data) + preview_max (per-track max band
// value for clamp-normalisation - NEVER divide by 127, A1 gotcha 3).

const PREVIEW_COLS = 120;
const PREVIEW_BYTES = PREVIEW_COLS * 3;

/** Decoded, render-ready preview strip for one track row. */
export interface PreviewStripData {
	/** Always 120 (contract-fixed). */
	cols: number;
	/** 360 bytes interleaved [low, mid, hi] per column. */
	bands: Uint8Array;
	/** Per-track normalisation divisor (server-computed max band value). */
	max: number;
}

/** Decode one row's preview pair. null preview_b64 = the real "no ANLZ
 * preview" state (fallback chain PWV6 -> PWV4 -> PWAV -> null exhausted)
 * and renders the explicit dash - never an invented strip. Any half-set
 * pair or wrong byte count is a contract violation and throws. */
export function decodePreviewStrip(
	preview_b64: string | null,
	preview_max: number | null
): PreviewStripData | null {
	if (preview_b64 === null) {
		if (preview_max !== null) {
			throw new Error('preview contract violation: preview_max set while preview_b64 is null');
		}
		return null;
	}
	if (preview_max === null) {
		throw new Error('preview contract violation: preview_b64 set while preview_max is null');
	}
	const bin = atob(preview_b64);
	if (bin.length !== PREVIEW_BYTES) {
		throw new Error(
			`preview_b64 decodes to ${bin.length} bytes - expected ${PREVIEW_BYTES} ` +
				`(${PREVIEW_COLS} cols x 3 bands)`
		);
	}
	const bands = new Uint8Array(PREVIEW_BYTES);
	let observedMax = 0;
	for (let i = 0; i < PREVIEW_BYTES; i++) {
		const v = bin.charCodeAt(i);
		bands[i] = v;
		if (v > observedMax) observedMax = v;
	}
	if (observedMax > 0 && preview_max <= 0) {
		throw new Error('preview contract violation: non-zero band data with preview_max <= 0');
	}
	return { cols: PREVIEW_COLS, bands, max: preview_max };
}

// ------------------------------- hydrated listings (contract 1, 2, 4)
// NOTE two contract-vs-convention bindings (flag to integrator if the
// backend chose differently): the contract lists "duration" and
// "comments"; per its own "follow existing models.py conventions" rule
// this client binds duration -> duration_ms (models.py TrackOut) and
// keeps comments verbatim (genre/comments are new browser-row fields,
// not TrackOut.notes).

/** One full track row from the hydrated playlist detail (contract 4). */
/** Browser Stems column: V=vocals, I=bass+other, D=drums. From listing hydrate. */
export type StemSummary =
	| { status: 'none' }
	| { status: 'invalid'; error?: string }
	| {
			status: 'ready';
			model: string | null;
			preset: string | null;
			overlap: number | null;
			shifts: number | null;
			format: string;
			total_bytes: number;
			groups: Record<string, { bytes: number; parts: string[] }>;
	  };

export function parseStemSummary(raw: unknown): StemSummary {
	if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
		throw new Error('stems summary must be an object');
	}
	const s = raw as { status?: unknown };
	if (s.status === 'none') return { status: 'none' };
	if (s.status === 'invalid') {
		const err = (raw as { error?: unknown }).error;
		return typeof err === 'string'
			? { status: 'invalid', error: err }
			: { status: 'invalid' };
	}
	if (s.status !== 'ready') {
		throw new Error(`stems summary: unknown status ${JSON.stringify(s.status)}`);
	}
	const r = raw as Record<string, unknown>;
	if (typeof r.format !== 'string' || typeof r.total_bytes !== 'number') {
		throw new Error('stems summary ready row missing format/total_bytes');
	}
	if (typeof r.groups !== 'object' || r.groups === null) {
		throw new Error('stems summary ready row missing groups');
	}
	return raw as StemSummary;
}

export interface CloudTransferWire {
	direction: 'upload' | 'download';
	bytes_transferred: number;
	/** Null only when the production transfer source cannot report a total. */
	bytes_total: number | null;
}

export interface PlaylistTrackRowWire {
	stable_id: string;
	/** v13 membership row id; playlist detail only (LIBM-21). */
	item_id?: string | null;
	title: string | null;
	artist: string | null;
	key: string | null;
	bpm: number | null;
	rating: number | null;
	energy: number | null;
	energy_source: 'mik' | null;
	energy_reason: string;
	key_status?: 'ok' | 'failed' | 'missing' | 'available-not-selected';
	key_reason?: string | null;
	bpm_status?: 'ok' | 'failed' | 'missing' | 'available-not-selected';
	bpm_reason?: string | null;
	bpm_source?: string | null;
	bpm_method?: string | null;
	bpm_confidence?: number | null;
	bpm_confidence_error?: string | null;
	loudness_status?: 'ok' | 'failed' | 'missing' | 'available-not-selected';
	loudness_reason?: string | null;
	duration_ms: number | null;
	genre: string | null;
	/** When genre is null, names why (missing tags extra, no file tag, etc.). */
	genre_reason?: string | null;
	/** GENRE-02: a JEV genre-family guess, served only while genre is empty. */
	genre_guess?: GenreGuess | null;
	comments: string | null;
	etag: string;
	preview_b64: string | null;
	preview_max: number | null;
	file_availability: FileAvailabilityStatus;
	file_exists: boolean | null;
	is_streaming: boolean;
	/** LIBUX-07: our own audio in non-local storage. Optional for older payloads. */
	is_remote?: boolean;
	/** LIBUX-13: a recorded remote copy, including when local audio also exists. */
	has_remote_copy: boolean;
	/** LIBUX-13: present only while this engine process is moving real bytes. */
	cloud_transfer: CloudTransferWire | null;
	/** Unmatched Spotify placeholder row (light green). Optional for older payloads. */
	spotify_pending?: boolean;
	quality: TrackQuality;
	play_count: number;
	/** Same four-status vocals as /anlz - drives PreviewStrip blue bars. */
	vocals: Vocals;
	/** Demucs bundle summary for the Stems column (V/I/D). */
	stems: StemSummary;
	/** Whether GET /tracks/{sid}/rb-meta can resolve for this track. False
	 * for a locally imported or djay-only track, whose rb-meta 404s BY
	 * CONTRACT - the browser skips the lazy per-row fetch rather than
	 * provoke a 404 the console logs unsuppressably on every visible row. */
	has_rb_mapping: boolean;
	artwork_available: boolean | null;
	artwork_status: 'ok' | 'no_image_path' | 'unresolved' | 'file_missing';
	lyrics?: LyricsRowSummary | null;
	is_remix?: boolean | null;
	is_radio_edit?: boolean | null;
}

/** `tracks` is Omit-ed off `PlaylistDetail` rather than narrowed, because the
 * two rows have DRIFTED: the schema's `TrackRowOut.spotify_pending` is a
 * required boolean (a pydantic default, so always on the wire) while this
 * module's `PlaylistTrackRowWire` still spells it optional for older payloads.
 * Closing that gap is api-rb's own call, so the drift is recorded here rather
 * than silently widened. */
export interface PlaylistDetailHydrated extends Omit<PlaylistDetail, 'tracks'> {
	/** Full rows in membership order - kills the per-row GET fan-out. */
	tracks: PlaylistTrackRowWire[];
}

/** GET /playlists/{id} with the hydrated track rows. Throws when the
 * tracks array is absent (backend contract point 4 not met). */
export async function getPlaylistHydrated(id: string): Promise<PlaylistDetailHydrated> {
	const detail = await _fetchJson<PlaylistDetailHydrated>(
		`/api/v1/playlists/${encodeURIComponent(id)}`
	);
	if (!Array.isArray(detail.tracks)) {
		throw new Error(
			`playlist ${id}: detail payload has no "tracks" array - backend contract point 4 not met`
		);
	}
	return detail;
}

// -------------------------------------------- global search (global-fts5-search)
// Whole-collection search (bm25-ranked FTS5 over title/artist/genre/comments/
// custom tags), as opposed to the browser's default within-pane client filter.

/** One search hit: the same hydrated row shape as a playlist-detail track
 * row, plus an excerpt of what matched. */
export interface SearchHitWire extends PlaylistTrackRowWire {
	match_context: string;
}

export interface SearchResultsWire {
	query: string;
	items: SearchHitWire[];
	total: number;
	next_offset: number | null;
}

/** GET /search - whole-collection search, not scoped to the active pane. */
export async function searchCollection(params: {
	q: string;
	limit?: number;
	offset?: number;
}): Promise<SearchResultsWire> {
	const qs = new URLSearchParams({ q: params.q });
	if (params.limit !== undefined) qs.set('limit', String(params.limit));
	if (params.offset !== undefined) qs.set('offset', String(params.offset));
	return _fetchJson<SearchResultsWire>(`/api/v1/search?${qs.toString()}`);
}

// -------------------------------------------- lyric-only search (Part 3 of #935, #1344)
// Matches cached synced lyrics only (never title/artist/genre/tags), over the
// durable background index Part 2 builds. Rendered below a divider AFTER the
// whole-collection search above settles - see LyricSearchResults.svelte.
//
// Reuses SearchHitWire/SearchResultsWire rather than declaring a
// near-identical sibling: a hit is a hydrated track row plus an excerpt of
// what matched either way. Here `match_context` is the one lyric line that
// best carries the query, never the full transcript or a bare title.

/** GET /lyrics/search - lyric-only search, not scoped to the active pane. */
export async function searchLyrics(params: {
	q: string;
	limit?: number;
	offset?: number;
}): Promise<SearchResultsWire> {
	const qs = new URLSearchParams({ q: params.q });
	if (params.limit !== undefined) qs.set('limit', String(params.limit));
	if (params.offset !== undefined) qs.set('offset', String(params.offset));
	return _fetchJson<SearchResultsWire>(`/api/v1/lyrics/search?${qs.toString()}`);
}

/** PlaylistSummary + contract point 2's available_count. */
export interface PlaylistSummaryHydrated extends PlaylistSummary {
	/** Tracks whose audio file exists on disk (bulk-stat pass, cached). */
	available_count: number;
}

/** GET /playlists with per-row available_count validated present. */
export async function listPlaylistsHydrated(opts?: {
	fast?: boolean;
}): Promise<PlaylistSummaryHydrated[]> {
	const qs = opts?.fast === true ? '?availability=skip' : '';
	const lists = await _fetchJson<PlaylistSummaryHydrated[]>(`/api/v1/playlists${qs}`);
	for (const p of lists) {
		if (
			typeof p.available_count !== 'number' ||
			!Number.isInteger(p.available_count) ||
			p.available_count < -1 ||
			p.available_count > p.track_count
		) {
			throw new Error(
				`playlist ${p.playlist_id}: invalid available_count - backend contract point 2 not met`
			);
		}
		if (opts?.fast === true) {
			if (p.available_count !== -1) {
				throw new Error(
					`playlist ${p.playlist_id}: fast list must return available_count=-1`
				);
			}
			continue;
		}
		if (p.available_count < 0) {
			throw new Error(
				`playlist ${p.playlist_id}: strict list must not return skipped available_count`
			);
		}
	}
	return lists;
}

export interface PlaylistTracksPageHydrated {
	tracks: PlaylistTrackRowWire[];
	total: number;
	next_offset: number | null;
}

/** GET /playlists/{id}/tracks with membership ETag (PERF-UI-05). */
export async function listPlaylistTracksPage(
	playlistId: string,
	params: { limit: number; offset: number }
): Promise<{ page: PlaylistTracksPageHydrated; etag: string }> {
	const qs = new URLSearchParams({
		limit: String(params.limit),
		offset: String(params.offset)
	});
	const path = `/api/v1/playlists/${encodeURIComponent(playlistId)}/tracks?${qs.toString()}`;
	const init: RequestInit = { headers: { Accept: 'application/json' } };
	const r = await fetch(`${RB_API_BASE}${path}`, init);
	if (!r.ok) await _throwRbApiError(r);
	const etag = r.headers.get('etag');
	if (!etag) {
		throw new Error(`playlist ${playlistId}: tracks page response carries no ETag header`);
	}
	const page = (await r.json()) as PlaylistTracksPageHydrated;
	if (!Array.isArray(page.tracks) || typeof page.total !== 'number') {
		throw new Error(`playlist ${playlistId}: tracks page payload malformed`);
	}
	return { page, etag };
}

/** Validated generated-contract summary of playable and broken library rows. */
export type ReconcileSummary = components['schemas']['ReconcileSummary'];

/** Fetch aggregate reconciliation counts without inventing a usable library state. */
export async function getReconcileSummary(): Promise<ReconcileSummary> {
	const summary = await unwrap(api.GET('/api/v1/reconcile/summary'));
	const { total_tracks, total_broken } = summary;
	if (
		typeof total_tracks !== 'number' ||
		typeof total_broken !== 'number' ||
		!Number.isInteger(total_tracks) ||
		!Number.isInteger(total_broken) ||
		total_tracks < 0 ||
		total_broken < 0 ||
		total_broken > total_tracks
	) {
		throw new Error('reconcile summary has invalid total_tracks or total_broken counts');
	}
	return summary;
}

/** Track listing item + contract point 1's per-row fields. STANDALONE-05
 * adds inline genre/genre_reason; is_streaming is not on the wire: the row
 * mapper settles it from `file_availability === 'streaming'` (issue #3934)
 * and otherwise leaves it lazy via rb-meta. */
/** GENRE-02 guess: never a tag, never written anywhere. */
export type GenreGuess = { family: string; confidence: number; source: 'jev' };

export type TrackListItemWire = Track & {
	genre?: string | null;
	genre_reason?: string | null;
	/** GENRE-02: a JEV genre-family guess, served only while genre is empty. */
	genre_guess?: GenreGuess | null;
	duration_ms?: number | null;
	bpm_source?: string | null;
	bpm_method?: string | null;
	bpm_confidence?: number | null;
	bpm_confidence_error?: string | null;
	energy: number | null;
	energy_source: 'mik' | null;
	energy_reason: string;
	preview_b64: string | null;
	preview_max: number | null;
	file_availability: FileAvailabilityStatus;
	file_exists: boolean | null;
	/** LIBUX-07: our own audio in non-local storage. Optional for older payloads. */
	is_remote?: boolean;
	/** LIBUX-13: a recorded remote copy, including when local audio also exists. */
	has_remote_copy: boolean;
	/** LIBUX-13: present only while this engine process is moving real bytes. */
	cloud_transfer: CloudTransferWire | null;
	quality: TrackQuality;
	play_count: number;
	vocals: Vocals;
	stems: StemSummary;
	/** See PlaylistTrackRowWire.has_rb_mapping - same flag, same purpose. */
	has_rb_mapping: boolean;
	artwork_available: boolean | null;
	artwork_status: 'ok' | 'no_image_path' | 'unresolved' | 'file_missing';
	lyrics?: LyricsRowSummary | null;
	is_remix?: boolean | null;
	is_radio_edit?: boolean | null;
};

export interface TracksPageHydrated {
	items: TrackListItemWire[];
	next_cursor: string | null;
}

/** GET /tracks with the inline preview/file_exists fields (contract 1)
 * and the ?available filter (contract 3, default all). */
export async function listTracksHydrated(params: {
	limit?: number;
	cursor?: string | undefined;
	available?: 'all' | 'true' | 'false';
	tag?: string;
}): Promise<TracksPageHydrated> {
	const qs = Object.entries(params)
		.filter(([, v]) => v !== undefined && v !== null && v !== '')
		.map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
		.join('&');
	const page = await _fetchJson<TracksPageHydrated>(`/api/v1/tracks${qs === '' ? '' : '?' + qs}`);
	for (const item of page.items) {
		if (
			item.file_availability !== 'AVAILABILITY_PENDING' &&
			typeof item.file_exists !== 'boolean'
		) {
			throw new Error(
				`track ${String(item.stable_id)}: listing row has no file_exists - ` +
					'backend contract point 1 not met'
			);
		}
		if (typeof item.file_availability !== 'string') {
			throw new Error(
				`track ${String(item.stable_id)}: listing row has no file_availability`
			);
		}
		// Loud, not falsy-defaulted: an absent flag would silently read as
		// 'no rekordbox mapping' and strip artwork off every row.
		if (typeof item.has_rb_mapping !== 'boolean') {
			throw new Error(
				`track ${String(item.stable_id)}: listing row has no has_rb_mapping - ` +
					'backend contract point 1 not met'
			);
		}
	}
	return page;
}

// ------------------------------------------------- the 4 new endpoints

/** GET /tracks/{sid}/anlz - waveforms, beatgrid, cues, phrases + vocals.
 * points bounds and default come from GET /api/v1/settings (runtime policy).
 * Validates the contract's vocals field up front (and primes the
 * vocalsOf memo) so paint code can trust it.
 * Concurrent callers with the same sid+points share one in-flight fetch so
 * a library prefetch and a deck load do not double-hit the backend. */
const _inflightAnlz = new Map<string, Promise<AnlzWithVocals>>();

/** The daemon's selected source is authoritative and can outlive this
 * document, so the ordinary path never permits the browser's shared HTTP
 * cache to reuse an /anlz payload from another tab or a prior reload, whose
 * document-local generation might coincidentally be the same
 * (discussion_r3923593660): `cache: 'no-store'` and the `gen` cache-buster
 * (anlz-fetch-generation.ts) both apply unconditionally, and `gen` stays
 * part of the in-flight key so a live source switch still splits concurrent
 * requests within this document.
 *
 * `bypassCache: true` additionally skips the in-flight de-dup below: an
 * authoritative recheck (a vendor mapping that may have landed after the
 * deck's own /anlz already served the empty local payload) must never be
 * handed a promise some unrelated in-flight call is already waiting on. */
export async function fetchAnlz(
	stable_id: string,
	points: number | null = defaultAnlzPoints(),
	bypassCache = false
): Promise<AnlzWithVocals> {
	const gen = currentAnlzFetchGeneration();
	const key = `${stable_id}:${points}:${gen}`;
	if (!bypassCache) {
		const existing = _inflightAnlz.get(key);
		if (existing !== undefined) return existing;
	}
	const pending = _fetchJson<AnlzWithVocals>(
		`/api/v1/tracks/${encodeURIComponent(stable_id)}/anlz?${anlzQuery(points, gen)}`,
		'no-store'
	).then((data) => {
		vocalsOf(data);
		return data;
	});
	if (bypassCache) return pending;
	const tracked = pending.finally(() => {
		if (_inflightAnlz.get(key) === tracked) _inflightAnlz.delete(key);
	});
	_inflightAnlz.set(key, tracked);
	return tracked;
}

/** Like {@link fetchAnlz}, but for the one caller (`refreshHotCues`,
 * audio-engine) that cannot accept a browser-HTTP-cache hit: the backend
 * marks a decoded /anlz response `private, no-cache` with an ETag over the
 * body (`rb_assets.py` `_CACHE_ANLZ`). That was `public, max-age=3600` when
 * this helper was written, and a plain `fetch` of the same URL inside the
 * hour could be satisfied from the HTTP cache with no round trip - "fresh" in
 * name only, right after the mutation it is meant to observe. `no-cache` now
 * forces a revalidation on every read, so the unconditional replay is gone;
 * `cache: 'reload'` additionally skips the conditional request, which keeps
 * this caller's guarantee independent of the server's cache-control policy
 * rather than resting on it. It re-primes the HTTP cache with the new
 * response, so ordinary reads right after this one still benefit from it. Deliberately bypasses the in-flight dedupe map above: an
 * ordinary in-flight `fetchAnlz` for the same key must not be handed this
 * stale-cache-tolerant promise, and vice versa.
 *
 * Includes the same `gen` cache-buster `fetchAnlz` reads (anlz-fetch-
 * generation.ts) so the URL it re-primes is the EXACT one an ordinary
 * `fetchAnlz` call issued after the same switch will request - without it,
 * this would prime a `gen`-less URL nothing else ever asks for, and every
 * subsequent read would still take a real round trip instead of benefiting
 * from this one. */
export async function fetchAnlzBypassingHttpCache(
	stable_id: string,
	points: number | null = defaultAnlzPoints()
): Promise<AnlzWithVocals> {
	const data = await _fetchJson<AnlzWithVocals>(
		`/api/v1/tracks/${encodeURIComponent(stable_id)}/anlz?${anlzQuery(points, currentAnlzFetchGeneration())}`,
		'reload'
	);
	vocalsOf(data);
	return data;
}

/** GET /tracks/{sid} with `cache: 'reload'`, paired with
 * `fetchAnlzBypassingHttpCache` on analysis-source switches: the openapi
 * client's ordinary `getTrack` can otherwise replay a pre-switch row while
 * `/anlz` already reflects the new lane. */
export async function fetchTrackBypassingHttpCache(stable_id: string): Promise<Track> {
	return _fetchJson<Track>(`/api/v1/tracks/${encodeURIComponent(stable_id)}`, 'reload');
}

/** GET /tracks/{sid}/rb-meta - vendor fields + file_exists/is_streaming flags. */
export async function fetchRbMeta(stable_id: string): Promise<RbMeta> {
	return _fetchJson<RbMeta>(`/api/v1/tracks/${encodeURIComponent(stable_id)}/rb-meta`);
}

/** GET /tracks/quality-ladder - the six venue rungs for the badge legend.
 * Fetched, never hardcoded: the ladder lives in audio_quality.py. */
export async function fetchQualityLadder(): Promise<QualityRung[]> {
	return _fetchJson<QualityRung[]>('/api/v1/tracks/quality-ladder');
}

// ------------------------------------------ hot-cue SAVE (djmdCue Kind 1-8)
// Write surface: apps/webui/server/rb_vendor.py save_hot_cue/clear_hot_cue.
// Slots beyond H (Kind 9-11) are unverified and never exposed here - the
// backend route param type rejects them with 422 before this client is
// even asked to serialize one.

export interface HotCueReversal {
	reversal_id: string;
}

export interface HotCueMutation {
	cue: AnlzCue | null;
	revision: string;
	reversal?: HotCueReversal;
}

export interface HotCueSlotState {
	slot: HotCueSlot;
	cue: AnlzCue | null;
	revision: string;
}

/** GET /tracks/{sid}/hot-cues - all slots, including empty-slot ETags. */
export async function fetchHotCueSlots(stable_id: string): Promise<HotCueSlotState[]> {
	return _fetchJson<HotCueSlotState[]>(
		`/api/v1/tracks/${encodeURIComponent(stable_id)}/hot-cues`
	);
}

/** CAS-save. The required revision comes from fetchHotCueSlots, and the
 * response carries a server-authoritative one-time undo token. */
export async function saveHotCue(
	stable_id: string,
	slot: HotCueSlot,
	in_ms: number,
	revision: string,
	comment?: string | null
): Promise<HotCueMutation> {
	return _putJson<HotCueMutation>(
		`/api/v1/tracks/${encodeURIComponent(stable_id)}/hot-cues/${slot}`,
		{ in_ms, comment: comment ?? null },
		revision
	);
}

/** CAS-clear with an explicit server-authoritative undo token. */
export async function clearHotCue(
	stable_id: string,
	slot: HotCueSlot,
	revision: string
): Promise<HotCueMutation> {
	return _deleteRequest<HotCueMutation>(
		`/api/v1/tracks/${encodeURIComponent(stable_id)}/hot-cues/${slot}`,
		revision
	);
}

/** Explicitly consume a server-created reversal token to undo one mutation. */
export async function restoreHotCue(
	stable_id: string,
	slot: HotCueSlot,
	revision: string,
	reversal_id: string
): Promise<HotCueMutation> {
	return _putJson<HotCueMutation>(
		`/api/v1/tracks/${encodeURIComponent(stable_id)}/hot-cues/${slot}/restore`,
		{ reversal_id },
		revision
	);
}

/** URL for GET /tracks/{sid}/artwork - use directly as <img src>. The
 * backend 404s ARTWORK_NOT_FOUND; consumers render the grey placeholder
 * slate on img error, never a fabricated image. */
export function artworkUrl(stable_id: string, size: ArtworkSize = 's'): string {
	return `${RB_API_BASE}/api/v1/tracks/${encodeURIComponent(stable_id)}/artwork?size=${size}`;
}

/** Artwork for a loaded deck: the same chain as {@link artworkUrl}, and when
 * the track has no local artwork (rekordbox, embedded picture, folder image)
 * the server also looks it up on MusicBrainz + Cover Art Archive and caches
 * what it finds. Only decks ask for this: the lookup is held to one request
 * per second, so a library page of rows must never trigger it. */
export function deckArtworkUrl(stable_id: string, size: ArtworkSize = 's'): string {
	return `${artworkUrl(stable_id, size)}&online=true`;
}

/** Human label for rb_meta.artwork_status when the art cell is empty.
 *
 * `ok`, null and undefined are all "no label", and undefined is the common
 * case rather than a defensive one: RbMetaOut (apps/webui/server/routes/
 * rb_assets.py) does not carry artwork_status at all, so every live row
 * arrives here without it. Anything OUTSIDE the declared union is a server
 * that grew a fifth status without telling the client, and that throws. */
export function artworkStatusLabel(
	status: 'ok' | 'no_image_path' | 'unresolved' | 'file_missing' | null | undefined
): string | null {
	switch (status) {
		case 'no_image_path':
			return 'no artwork path in library';
		case 'unresolved':
			return 'artwork path unresolved';
		case 'file_missing':
			return 'artwork file missing';
		case 'ok':
		case null:
		case undefined:
			return null;
		default: {
			const _exhaustive: never = status;
			throw new Error(`unhandled artwork status: ${String(_exhaustive)}`);
		}
	}
}

/** URL for GET /tracks/{sid}/audio (Range-capable stream). */
export function audioUrl(stable_id: string): string {
	return `${RB_API_BASE}/api/v1/tracks/${encodeURIComponent(stable_id)}/audio`;
}

/** Fetch the full audio file as an ArrayBuffer for decodeAudioData.
 * Throws RbApiError on AUDIO_FILE_MISSING / AUDIO_IS_STREAMING_URI /
 * TRACK_NOT_FOUND - the audio engine surfaces these, never plays silence. */
export async function fetchAudioArrayBuffer(stable_id: string): Promise<ArrayBuffer> {
	const r = await fetch(audioUrl(stable_id));
	if (!r.ok) await _throwRbApiError(r);
	return r.arrayBuffer();
}

// ------------------------------------------------ precomputed Demucs stems

export const DEMUCS_STEM_PARTS = ['vocals', 'drums', 'bass', 'other'] as const;
export type DemucsStemPart = (typeof DEMUCS_STEM_PARTS)[number];

/** Part names across every layout. `instrumental` is RoFormer's single
 * not-vocals part, which already contains drums and bass. */
export const ALL_STEM_PARTS = ['vocals', 'drums', 'bass', 'other', 'instrumental'] as const;
export type StemPartName = (typeof ALL_STEM_PARTS)[number];

export const STEM_LAYOUT_PART_NAMES = {
	demucs4: ['vocals', 'drums', 'bass', 'other'],
	roformer2: ['vocals', 'instrumental']
} as const satisfies Record<string, readonly StemPartName[]>;

export type StemLayoutName = keyof typeof STEM_LAYOUT_PART_NAMES;

const _STEM_MEDIA_TYPES = ['audio/wav', 'audio/flac', 'audio/ogg', 'audio/mpeg'] as const;

export interface StemArtifactManifest {
	schema: 1;
	stable_id: string;
	source: 'demucs' | 'roformer';
	model: string;
	layout: StemLayoutName;
	sample_rate_hz: number;
	frame_count: number;
	channel_count: number;
	parts: Partial<Record<StemPartName, { media_type: (typeof _STEM_MEDIA_TYPES)[number] }>>;
}

export type StemArtifactProbe =
	| { status: 'ready'; manifest: StemArtifactManifest }
	| { status: 'unavailable'; error: string }
	// The server has the bundle in its R2 index and just started fetching it
	// (STEM_BUNDLE_HYDRATING). NOT settled: the same GET answers `ready` once
	// the download lands, so a caller must re-ask, never read this as "no stems".
	| { status: 'hydrating'; error: string };

function _validateStemManifest(raw: unknown, stableId: string): StemArtifactManifest {
	if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
		throw new TypeError('stem manifest must be an object');
	}
	const manifest = raw as Partial<StemArtifactManifest>;
	if (manifest.schema !== 1) throw new Error(`stem manifest schema must be 1, got ${String(manifest.schema)}`);
	if (manifest.stable_id !== stableId) {
		throw new Error(`stem manifest stable_id mismatch: expected ${stableId}, got ${String(manifest.stable_id)}`);
	}
	if (manifest.source !== 'demucs' && manifest.source !== 'roformer') {
		throw new Error(`stem manifest source must be demucs or roformer, got ${String(manifest.source)}`);
	}
	// Layout is REQUIRED: the part set is read from it, never guessed from the
	// keys, so a manifest that forgot to declare it is rejected rather than
	// silently treated as the 4-part default.
	if (manifest.layout !== 'demucs4' && manifest.layout !== 'roformer2') {
		throw new Error(`stem manifest layout must be demucs4 or roformer2, got ${String(manifest.layout)}`);
	}
	if (typeof manifest.model !== 'string' || manifest.model.trim() === '') {
		throw new Error('stem manifest model must be a non-empty string');
	}
	for (const field of ['sample_rate_hz', 'frame_count', 'channel_count'] as const) {
		const value = manifest[field];
		if (typeof value !== 'number' || !Number.isInteger(value) || value <= 0) {
			throw new Error(`stem manifest ${field} must be a positive integer`);
		}
	}
	if (typeof manifest.parts !== 'object' || manifest.parts === null || Array.isArray(manifest.parts)) {
		throw new Error('stem manifest parts must be an object');
	}
	const partKeys = Object.keys(manifest.parts).sort();
	const expectedPartKeys = [...STEM_LAYOUT_PART_NAMES[manifest.layout]].sort();
	if (
		partKeys.length !== expectedPartKeys.length ||
		partKeys.some((key, index) => key !== expectedPartKeys[index])
	) {
		throw new Error(
			`stem manifest parts must contain exactly ${expectedPartKeys.join(', ')} for ${manifest.layout}`
		);
	}
	for (const part of expectedPartKeys) {
		const media = manifest.parts[part]?.media_type;
		if (!(_STEM_MEDIA_TYPES as readonly string[]).includes(media as string)) {
			throw new Error(
				`stem manifest ${part} media_type must be one of ${_STEM_MEDIA_TYPES.join(', ')}`
			);
		}
	}
	return manifest as StemArtifactManifest;
}

/** Probe the optional precomputed artifact capability. A 404 or HTTP 200
 * unavailable envelope is published as explicit unavailable state; malformed
 * or broken artifacts still reject. */
export async function probeStemArtifact(stableId: string): Promise<StemArtifactProbe> {
	if (optionalResources(stableId).stems === false) {
		return { status: 'unavailable', error: 'no stem bundle advertised' };
	}
	try {
		const raw = await _fetchJson<unknown>(
			`/api/v1/tracks/${encodeURIComponent(stableId)}/stems`
		);
		if (
			typeof raw === 'object' &&
			raw !== null &&
			'status' in raw &&
			(raw as { status: unknown }).status === 'unavailable'
		) {
			const code =
				'code' in raw ? String((raw as { code: unknown }).code) : 'STEM_BUNDLE_NOT_FOUND';
			const message =
				'message' in raw ? String((raw as { message: unknown }).message) : 'no stem bundle';
			const hydrating =
				code === 'STEM_BUNDLE_HYDRATING' ||
				('hydrating' in raw && (raw as { hydrating: unknown }).hydrating === true);
			return { status: hydrating ? 'hydrating' : 'unavailable', error: `${code}: ${message}` };
		}
		return { status: 'ready', manifest: _validateStemManifest(raw, stableId) };
	} catch (error) {
		if (error instanceof RbApiError && error.status === 404) {
			return { status: 'unavailable', error: error.message };
		}
		throw error;
	}
}

export function stemAudioUrl(stableId: string, part: StemPartName): string {
	return (
		`${RB_API_BASE}/api/v1/tracks/${encodeURIComponent(stableId)}/stems/` +
		encodeURIComponent(part)
	);
}

/** Fetch exactly the parts THIS layout declares. Fetching the fixed four
 * against a 2-part bundle would 404 on drums/bass/other and fail a load that
 * is actually fine. */
export async function fetchStemAudioArrayBuffers(
	stableId: string,
	layout: StemLayoutName = 'demucs4'
): Promise<Partial<Record<StemPartName, ArrayBuffer>>> {
	const entries = await Promise.all(
		STEM_LAYOUT_PART_NAMES[layout].map(async (part) => {
			const response = await fetch(stemAudioUrl(stableId, part), { signal: stemWorkSignal() });
			if (!response.ok) await _throwRbApiError(response);
			return [part, await response.arrayBuffer()] as const;
		})
	);
	return Object.fromEntries(entries) as Partial<Record<StemPartName, ArrayBuffer>>;
}

// --------------------------------------------- voice probe (text-command-entry)

/** POST /voice/probe response shape (apps/webui/server/routes/voice_probe.py). */
export interface VoiceProbeResult {
	transcript: string;
	intent: string | null;
	slots: Record<string, unknown>;
	blocked: boolean;
	reason: string | null;
	reply: string | null;
	client_action: 'browser_search' | null;
	probe_only: boolean;
}

/** Text-command entry: sends free text through the apps/voice grammar
 * parser (no mic, no daemon). Destructive intents (SAVE_CUE, RATE_TRACK)
 * come back with blocked: true and are never executed server-side. */
export async function probeVoiceCommand(text: string): Promise<VoiceProbeResult> {
	const r = await fetch(`${RB_API_BASE}/api/v1/voice/probe`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
		body: JSON.stringify({ text })
	});
	if (!r.ok) await _throwRbApiError(r);
	return (await r.json()) as VoiceProbeResult;
}

// ---------------------------------------------- stem separation tier ladder
// Backend: apps/webui/server/routes/stem_tiers.py, config: apps/stems/tiers.py.
// The ladder is LOCAL -> quick -> optimal -> delicious. `quick` currently ships
// NOT_APPLICABLE: it was measured and saves 0.7s over optimal, so it stays on
// the ladder to record that the gap was looked at, and renders inert.

export type StemTier = {
	key: string;
	name: string;
	where: 'local' | 'modal';
	preset_tag: string;
	model: string;
	overlap: number;
	shifts: number;
	gpu: string;
	purpose: string;
	evidence: string;
	evidence_strength: 'MEASURED' | 'PARTIAL' | 'UNMEASURED';
	availability: 'AVAILABLE' | 'NOT_APPLICABLE';
	unavailable_because: string;
	is_default: boolean;
	/** false when THIS engine refuses to spawn the tier (a Modal tier in the
	 * installed app); the menu hides it (INSTALL-32). */
	runnable_here: boolean;
	not_runnable_because: string | null;
};

export type StemTierEstimate = {
	tier: string;
	name: string;
	where: 'local' | 'modal';
	gpu: string;
	availability: string;
	unavailable_because: string;
	/** false means NOT BENCHMARKED. Render the reason, never a guessed number. */
	measured: boolean;
	seconds: number | null;
	usd: number | null;
	measured_at: string | null;
	n_tracks: number | null;
	r_squared: number | null;
	unavailable_reason: string | null;
};

export type StemJob = {
	job_id: string;
	stable_id: string;
	tier: string;
	state: 'running' | 'done' | 'failed';
	returncode: number | null;
	command: string;
	log_tail: string;
};

/** GET /stems/tiers - the ladder in render order, NOT_APPLICABLE rungs included. */
export async function fetchStemTiers(): Promise<StemTier[]> {
	return _fetchJson<StemTier[]>('/api/v1/stems/tiers');
}

/** GET /stems/estimate - every rung costed for one track, one round trip. */
export async function fetchStemEstimates(
	durationSeconds: number
): Promise<{ duration_s: number; tiers: StemTierEstimate[] }> {
	return _fetchJson(`/api/v1/stems/estimate?seconds=${encodeURIComponent(durationSeconds)}`);
}

/** POST /stems/generate - start a REAL separation. Returns a job to poll. */
export async function startStemGeneration(
	stable_id: string,
	tier: string
): Promise<{ job_id: string; tier: string; command: string; poll: string }> {
	const r = await fetch(`${RB_API_BASE}/api/v1/stems/generate`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
		body: JSON.stringify({ stable_id, tier })
	});
	if (!r.ok) await _throwRbApiError(r);
	return await r.json();
}

/** GET /stems/jobs/{id} - live state, including the real returncode. */
export async function fetchStemJob(job_id: string): Promise<StemJob> {
	return _fetchJson<StemJob>(`/api/v1/stems/jobs/${encodeURIComponent(job_id)}`);
}

// --------------------------------------------- vocal analysis trigger (PARITY-08)

export interface VocalsAnalyzeResult {
	claimed: string[];
	refused: Record<string, string>;
}

/** POST /vocals/analyze - derive vocals from an existing stem bundle (CPU,
 * no demucs). Refuses (not silently skips) any track outside the classifier's
 * `todo` category; check `result.refused[stable_id]` for the reason. */
export async function analyzeVocalsFromStems(stable_id: string): Promise<VocalsAnalyzeResult> {
	const r = await fetch(`${RB_API_BASE}/api/v1/vocals/analyze`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
		body: JSON.stringify({ stable_ids: [stable_id], mode: 'from-stems' })
	});
	if (!r.ok) await _throwRbApiError(r);
	return await r.json();
}
