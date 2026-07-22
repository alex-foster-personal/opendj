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
import type { AnlzData, ArtworkSize, RbMeta } from './types';

// Re-export the existing hand-written client (RECON-FRONTEND 3).
export {
	ConflictError,
	getHealth,
	getPlaylist,
	getTrack,
	listPlaylists,
	listTracks,
	patchTrack
} from '$lib/api';
export type {
	HealthOut,
	PlaylistDetail,
	PlaylistSummary,
	Track,
	TracksPage
} from '$lib/api';

export const RB_API_BASE: string = API_BASE;

/** Explicit backend error: HTTP status + the contract's detail.code. */
export class RbApiError extends Error {
	constructor(
		public status: number,
		public code: string,
		message: string
	) {
		super(`${code}: ${message}`);
		this.name = 'RbApiError';
	}
}

// ----------------------------------------------------------- _helpers

async function _throwRbApiError(r: Response): Promise<never> {
	// Backend contract: errors are explicit JSON {"detail": {code, message}}
	// (COMPONENT-MAP 2 shared plumbing). If the body is not that shape the
	// json()/field access fails loudly, which is the behaviour we want.
	const body = (await r.json()) as { detail?: { code?: string; message?: string } | string };
	const detail = typeof body.detail === 'object' && body.detail !== null ? body.detail : undefined;
	throw new RbApiError(
		r.status,
		detail?.code ?? `HTTP_${r.status}`,
		detail?.message ?? r.statusText
	);
}

async function _fetchJson<T>(path: string): Promise<T> {
	const r = await fetch(`${RB_API_BASE}${path}`, { headers: { Accept: 'application/json' } });
	if (!r.ok) await _throwRbApiError(r);
	return (await r.json()) as T;
}

// --------------------------------------------- /anlz vocals (SPIKE-B1)
// Shared contract point 5: GET /tracks/{sid}/anlz gains field "vocals",
// exactly one of the three states below. The three UI states are
// MANDATORY (bars / analyzed-no-vocals / not-analyzed) - bars are never
// rendered from anything but real PVDI-derived regions.

/** One vocal region: a run of PVDI intensity >= VOCAL_INTENSITY_MIN.
 * intensity is the max PVDI value (1..4) within the run. */
export interface VocalRegion {
	start_s: number;
	end_s: number;
	intensity: number;
}

export type Vocals =
	| { status: 'rekordbox'; fps: number; regions: VocalRegion[] }
	| { status: 'no_vocals'; fps: number; regions: VocalRegion[] } // regions always []
	| { status: 'not_analyzed' };

/** AnlzData plus the contract's vocals field. types.ts is the frozen
 * build-unit contract, so the widening lives here (api-rb owns types). */
export type AnlzWithVocals = AnlzData & { vocals: Vocals };

const _vocalsMemo = new WeakMap<AnlzData, Vocals>();

function _validateVocals(raw: unknown): Vocals {
	if (typeof raw !== 'object' || raw === null) {
		throw new Error(
			'anlz payload has no valid "vocals" field - backend contract point 5 not met'
		);
	}
	const v = raw as { status?: unknown; fps?: unknown; regions?: unknown };
	if (v.status === 'not_analyzed') return { status: 'not_analyzed' };
	if (v.status !== 'rekordbox' && v.status !== 'no_vocals') {
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
	const vocals = _validateVocals((anlz as AnlzData & { vocals?: unknown }).vocals);
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
export interface PlaylistTrackRowWire {
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
}

export interface PlaylistDetailHydrated extends PlaylistDetail {
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

/** PlaylistSummary + contract point 2's available_count. */
export interface PlaylistSummaryHydrated extends PlaylistSummary {
	/** Tracks whose audio file exists on disk (bulk-stat pass, cached). */
	available_count: number;
}

/** GET /playlists with per-row available_count validated present. */
export async function listPlaylistsHydrated(): Promise<PlaylistSummaryHydrated[]> {
	const lists = await _fetchJson<PlaylistSummaryHydrated[]>('/api/v1/playlists');
	for (const p of lists) {
		if (typeof p.available_count !== 'number') {
			throw new Error(
				`playlist ${p.playlist_id}: no available_count - backend contract point 2 not met`
			);
		}
	}
	return lists;
}

/** Track listing item + contract point 1's per-row fields. is_streaming
 * and genre are NOT in the listing contract (playlist rows only), hence
 * absent here - the browser falls back to lazy rb-meta for those. */
export type TrackListItemWire = Track & {
	duration_ms?: number | null;
	preview_b64: string | null;
	preview_max: number | null;
	file_exists: boolean;
};

export interface TracksPageHydrated {
	items: TrackListItemWire[];
	next_cursor: string | null;
}

/** GET /tracks with the inline preview/file_exists fields (contract 1)
 * and the ?available filter (contract 3, default all). */
export async function listTracksHydrated(params: {
	limit?: number;
	cursor?: string;
	available?: 'all' | 'true' | 'false';
}): Promise<TracksPageHydrated> {
	const qs = Object.entries(params)
		.filter(([, v]) => v !== undefined && v !== null && v !== '')
		.map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
		.join('&');
	const page = await _fetchJson<TracksPageHydrated>(`/api/v1/tracks${qs === '' ? '' : '?' + qs}`);
	for (const item of page.items) {
		if (typeof item.file_exists !== 'boolean') {
			throw new Error(
				`track ${String(item.stable_id)}: listing row has no file_exists - ` +
					'backend contract point 1 not met'
			);
		}
	}
	return page;
}

// ------------------------------------------------- the 4 new endpoints

/** GET /tracks/{sid}/anlz - waveforms, beatgrid, cues, phrases + vocals.
 * points: 100..2400, default 2400 (server downsamples detail bands).
 * Validates the contract's vocals field up front (and primes the
 * vocalsOf memo) so paint code can trust it. */
export async function fetchAnlz(stable_id: string, points = 2400): Promise<AnlzWithVocals> {
	const data = await _fetchJson<AnlzWithVocals>(
		`/api/v1/tracks/${encodeURIComponent(stable_id)}/anlz?points=${points}`
	);
	vocalsOf(data);
	return data;
}

/** GET /tracks/{sid}/rb-meta - vendor fields + file_exists/is_streaming flags. */
export async function fetchRbMeta(stable_id: string): Promise<RbMeta> {
	return _fetchJson<RbMeta>(`/api/v1/tracks/${encodeURIComponent(stable_id)}/rb-meta`);
}

/** URL for GET /tracks/{sid}/artwork - use directly as <img src>. The
 * backend 404s ARTWORK_NOT_FOUND; consumers render the grey placeholder
 * slate on img error, never a fabricated image. */
export function artworkUrl(stable_id: string, size: ArtworkSize = 's'): string {
	return `${RB_API_BASE}/api/v1/tracks/${encodeURIComponent(stable_id)}/artwork?size=${size}`;
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
