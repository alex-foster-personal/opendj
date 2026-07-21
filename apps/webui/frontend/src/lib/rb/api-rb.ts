/**
 * Typed fetch helpers for the 4 NEW rb-assets endpoints
 * (COMPONENT-MAP.md section 2), plus re-exports of the existing
 * track/playlist client so /performance builders import everything
 * API-shaped from this one module.
 *
 * Base URL: mirrors src/lib/api.ts (relative '/api/...' in the browser via
 * the Vite dev proxy / prod same-origin; absolute host during SSR), with a
 * VITE_API_BASE env override supported in THIS file only.
 *
 * Fail-fast: every helper checks r.ok and throws RbApiError carrying the
 * backend's explicit {"detail": {code, message}} payload. No silent
 * fallbacks, no invented data.
 */

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

const DEFAULT_BASE: string = typeof window === 'undefined' ? 'http://127.0.0.1:8585' : '';
const ENV_BASE: string | undefined = import.meta.env.VITE_API_BASE as string | undefined;
export const RB_API_BASE: string = ENV_BASE ?? DEFAULT_BASE;

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

// ------------------------------------------------- the 4 new endpoints

/** GET /tracks/{sid}/anlz - waveforms, beatgrid, cues, phrases.
 * points: 100..2400, default 2400 (server downsamples detail bands). */
export async function fetchAnlz(stable_id: string, points = 2400): Promise<AnlzData> {
	return _fetchJson<AnlzData>(
		`/api/v1/tracks/${encodeURIComponent(stable_id)}/anlz?points=${points}`
	);
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
