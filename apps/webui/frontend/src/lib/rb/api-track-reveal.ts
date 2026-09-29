/** Thin client for POST /api/v1/tracks/{stable_id}:reveal (issue #2286). */

import { API_BASE, RbApiError } from '$lib/api';
import { refuseStickRead } from './track-source';

export async function revealTrack(stableId: string): Promise<void> {
	// Spec 4b: reveal resolves a library file; no stick route exists.
	refuseStickRead(stableId, 'reveal');
	const r = await fetch(
		`${API_BASE}/api/v1/tracks/${encodeURIComponent(stableId)}:reveal`,
		{ method: 'POST' }
	);
	if (!r.ok) {
		const body = (await r.json()) as {
			detail?: { code?: string; message?: string } | string;
		};
		const detail =
			typeof body.detail === 'object' && body.detail !== null ? body.detail : undefined;
		const msg = typeof body.detail === 'string' ? body.detail : detail?.message;
		throw new RbApiError(r.status, detail?.code ?? `HTTP_${r.status}`, msg ?? r.statusText);
	}
}
