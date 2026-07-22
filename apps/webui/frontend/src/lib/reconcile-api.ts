/**
 * Typed fetch wrappers for the reconcile-router LANE (read-only broken-track
 * listing + relocate-files candidate finder/apply). Kept as a dedicated file
 * rather than extending `api.ts` so the two lane members building on this
 * contract in parallel (missing-tracks-folder, relocate-files) don't collide
 * on the same file.
 */

import { API_BASE } from './api';

export interface BrokenTrack {
	stable_id: string;
	title: string | null;
	artist: string | null;
	album: string | null;
	bpm: number | null;
	key: string | null;
	rating: number | null;
	duration_ms: number | null;
	original_path: string;
	basename: string;
	parent_dir: string;
	vendor_id: string | null;
	playlist_ids: string[];
	file_exists: boolean;
	is_streaming: boolean;
}

export interface BrokenTrackList {
	total: number;
	tracks: BrokenTrack[];
}

export interface RelocateCandidate {
	path: string;
	confidence: number;
	signals: string[];
	triple_validated: boolean;
}

export interface RelocateCandidateList {
	stable_id: string;
	original_path: string | null;
	vendor_id: string | null;
	total: number;
	candidates: RelocateCandidate[];
}

export interface RelocateApplyResult {
	stable_id: string;
	new_path: string;
	target: 'rekordbox' | 'state';
	vendor_id: string | null;
	backup_path: string | null;
}

export class RelocateApplyError extends Error {
	constructor(public status: number, public code: string, message: string) {
		super(message);
	}
}

async function request(path: string, init: RequestInit = {}): Promise<Response> {
	return fetch(`${API_BASE}${path}`, {
		...init,
		headers: {
			Accept: 'application/json',
			'Content-Type': 'application/json',
			...(init.headers || {})
		}
	});
}

export async function listBroken(playlistId?: string): Promise<BrokenTrackList> {
	const qs = playlistId ? `?playlist_id=${encodeURIComponent(playlistId)}` : '';
	const r = await request(`/api/v1/reconcile/broken${qs}`);
	if (!r.ok) throw new Error(`list broken failed: ${r.status}`);
	return r.json();
}

export async function getRelocateCandidates(
	stableId: string,
	limit = 5
): Promise<RelocateCandidateList> {
	const r = await request(
		`/api/v1/relocate/candidates/${encodeURIComponent(stableId)}?limit=${limit}`
	);
	if (!r.ok) throw new Error(`get candidates failed: ${r.status}`);
	return r.json();
}

export async function applyRelocate(
	stableId: string,
	newPath: string,
	opts: { ifMatch: string; expectedOriginalPath: string }
): Promise<RelocateApplyResult> {
	const r = await request(`/api/v1/relocate/${encodeURIComponent(stableId)}/apply`, {
		method: 'POST',
		headers: { 'If-Match': opts.ifMatch },
		body: JSON.stringify({
			new_path: newPath,
			expected_original_path: opts.expectedOriginalPath,
			confirm: true
		})
	});
	if (!r.ok) {
		let code = 'UNKNOWN';
		let message = `apply failed: ${r.status}`;
		try {
			const body = await r.json();
			if (typeof body?.detail === 'object' && body.detail) {
				code = body.detail.code ?? code;
				message = body.detail.message ?? message;
			} else if (body?.message) {
				message = body.message;
			}
		} catch {
			// non-JSON error body; keep the generic message
		}
		throw new RelocateApplyError(r.status, code, message);
	}
	return r.json();
}
