/**
 * Playlist membership write client (add-remove-reorder-tracks node).
 *
 * The backend's PUT /playlists/{id}/tracks is the single membership
 * primitive (see apps/webui/server/routes/playlist_write.py + playlist_store.py
 * docstrings): add / remove / reorder are all expressed as one full-list
 * replace with If-Match optimistic concurrency. This module is a small,
 * standalone client for it - kept out of api-rb.ts (single-owner hotspot
 * per FANOUT-CONVENTIONS.md) even though it type-imports from there.
 *
 * GET /playlists/{id} now also carries an ETag header (the read route was
 * extended alongside this client so a caller that only reads through the
 * hydrated detail endpoint can still mutate membership without a second
 * round trip through the write router).
 */

import { API_BASE } from '$lib/api';
import type { PlaylistDetailHydrated } from '$lib/rb/api-rb';

/** Minimal playlist row shape the write endpoints echo back (PlaylistWriteOut
 * / the 409 conflict body's "current" - apps/webui/server/routes/playlist_write.py). */
export interface PlaylistRowWire {
	playlist_id: string;
	name: string;
	vendor: string;
	vendor_pl_id: string;
	items: string[];
	track_count?: number;
	created_at: string;
	updated_at: string;
}

/** Mirrors backend errors.py ConflictBody: {error: "conflict", message, current, etag}. */
export class PlaylistConflictError extends Error {
	constructor(
		public current: PlaylistRowWire,
		public etag: string
	) {
		super('playlist If-Match mismatch');
		this.name = 'PlaylistConflictError';
	}
}

async function _errorMessage(r: Response): Promise<string> {
	try {
		const body = (await r.json()) as { message?: string };
		return body.message ?? r.statusText;
	} catch {
		return r.statusText;
	}
}

/** GET /playlists/{id} with the hydrated detail plus the ETag response
 * header the next mutation must send as If-Match. Fails loudly (matching
 * the rest of the rb client) rather than mutating against a guessed etag. */
export async function getPlaylistTracksEtag(
	playlistId: string
): Promise<{ detail: PlaylistDetailHydrated; etag: string }> {
	const r = await fetch(`${API_BASE}/api/v1/playlists/${encodeURIComponent(playlistId)}`, {
		headers: { Accept: 'application/json' }
	});
	if (!r.ok) throw new Error(`GET playlist ${playlistId} failed (${r.status}): ${await _errorMessage(r)}`);
	const etag = r.headers.get('etag');
	if (!etag) {
		throw new Error(`playlist ${playlistId}: GET response carries no ETag header`);
	}
	const detail = (await r.json()) as PlaylistDetailHydrated;
	if (!Array.isArray(detail.tracks)) {
		throw new Error(`playlist ${playlistId}: detail payload has no "tracks" array`);
	}
	return { detail, etag };
}

export interface PlaylistWriteResult {
	items: string[];
	etag: string;
}

/** PUT /playlists/{id}/tracks - full membership replace, backing add /
 * remove / reorder alike. Throws PlaylistConflictError on a stale If-Match
 * (409) so the caller reloads and lets the user retry rather than silently
 * clobbering an intervening change. */
export async function replacePlaylistTracks(
	playlistId: string,
	etag: string,
	stableIds: string[]
): Promise<PlaylistWriteResult> {
	const r = await fetch(`${API_BASE}/api/v1/playlists/${encodeURIComponent(playlistId)}/tracks`, {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'If-Match': etag },
		body: JSON.stringify({ stable_ids: stableIds })
	});
	if (r.status === 409) {
		const body = (await r.json()) as { current: PlaylistRowWire; etag: string };
		throw new PlaylistConflictError(body.current, body.etag);
	}
	if (!r.ok) throw new Error(`update playlist ${playlistId} failed (${r.status}): ${await _errorMessage(r)}`);
	const fresh = r.headers.get('etag');
	if (!fresh) throw new Error(`playlist ${playlistId}: PUT response carries no ETag header`);
	const out = (await r.json()) as PlaylistRowWire;
	return { items: out.items, etag: fresh };
}

/** POST /playlists - create empty playlist (201 + ETag). */
export async function createPlaylist(name: string): Promise<PlaylistRowWire> {
	const r = await fetch(`${API_BASE}/api/v1/playlists`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
		body: JSON.stringify({ name })
	});
	if (!r.ok) throw new Error(`create playlist failed (${r.status}): ${await _errorMessage(r)}`);
	return (await r.json()) as PlaylistRowWire;
}

/** PATCH /playlists/{id} - rename (If-Match required). */
export async function renamePlaylist(
	playlistId: string,
	etag: string,
	name: string
): Promise<PlaylistRowWire> {
	const r = await fetch(`${API_BASE}/api/v1/playlists/${encodeURIComponent(playlistId)}`, {
		method: 'PATCH',
		headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'If-Match': etag },
		body: JSON.stringify({ name })
	});
	if (r.status === 409) {
		const body = (await r.json()) as { current: PlaylistRowWire; etag: string };
		throw new PlaylistConflictError(body.current, body.etag);
	}
	if (!r.ok) throw new Error(`rename playlist ${playlistId} failed (${r.status}): ${await _errorMessage(r)}`);
	return (await r.json()) as PlaylistRowWire;
}

/** DELETE /playlists/{id} (If-Match required) -> 204. */
export async function deletePlaylist(playlistId: string, etag: string): Promise<void> {
	const r = await fetch(`${API_BASE}/api/v1/playlists/${encodeURIComponent(playlistId)}`, {
		method: 'DELETE',
		headers: { Accept: 'application/json', 'If-Match': etag }
	});
	if (r.status === 409) {
		const body = (await r.json()) as { current: PlaylistRowWire; etag: string };
		throw new PlaylistConflictError(body.current, body.etag);
	}
	if (!r.ok && r.status !== 204) {
		throw new Error(`delete playlist ${playlistId} failed (${r.status}): ${await _errorMessage(r)}`);
	}
}
