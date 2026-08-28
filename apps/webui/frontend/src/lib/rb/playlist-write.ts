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
 *
 * CONVERTED onto the generated OpenAPI client (src/lib/api/client.ts).
 * Transport only: the exported signatures, PlaylistConflictError contract
 * and the loud ETag/tracks runtime checks are unchanged. PlaylistRowWire
 * stays hand-written rather than aliasing PlaylistWriteOut: the generated
 * schema requires track_count while this wire shape keeps it optional
 * (the 409 conflict body's "current" row), so the fields do not match
 * exactly.
 */

import type { PlaylistDetailHydrated } from '$lib/rb/api-rb';

import { ApiError, api, unwrap } from '../api/client';

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

/** The write router's non-409 errors carry a TOP-LEVEL {message} (errors.py
 * ErrorBody), not the {"detail": {...}} envelope ApiError decodes, so read
 * the parsed body first and fall back to ApiError's own message. */
function _messageOf(error: ApiError): string {
	return (error.body as { message?: string } | null)?.message ?? error.message;
}

/** GET /playlists/{id} with the hydrated detail plus the ETag response
 * header the next mutation must send as If-Match. Fails loudly (matching
 * the rest of the rb client) rather than mutating against a guessed etag. */
export async function getPlaylistTracksEtag(
	playlistId: string
): Promise<{ detail: PlaylistDetailHydrated; etag: string }> {
	let data: unknown;
	let response: Response;
	try {
		({ data, response } = await api.GET('/api/v1/playlists/{playlist_id}', {
			params: { path: { playlist_id: playlistId } }
		}));
	} catch (error) {
		if (error instanceof ApiError) {
			throw new Error(`GET playlist ${playlistId} failed (${error.status}): ${_messageOf(error)}`);
		}
		throw error;
	}
	const etag = response.headers.get('etag');
	if (!etag) {
		throw new Error(`playlist ${playlistId}: GET response carries no ETag header`);
	}
	const detail = data as unknown as PlaylistDetailHydrated;
	if (!Array.isArray(detail.tracks)) {
		throw new Error(`playlist ${playlistId}: detail payload has no "tracks" array`);
	}
	return { detail, etag };
}

export interface PlaylistWriteResult {
	items: string[];
	etag: string;
}

/** Map a mutation failure onto this module's contract: a stale If-Match
 * (409, top-level ConflictBody) becomes PlaylistConflictError; any other
 * daemon answer keeps the old "<verb> playlist ... failed (status): message"
 * Error; a network fault is rethrown untouched. */
function _throwWriteError(error: unknown, describe: string): never {
	if (error instanceof ApiError && error.status === 409) {
		const body = error.body as { current: PlaylistRowWire; etag: string };
		throw new PlaylistConflictError(body.current, body.etag);
	}
	if (error instanceof ApiError) {
		throw new Error(`${describe} failed (${error.status}): ${_messageOf(error)}`);
	}
	throw error;
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
	let data: unknown;
	let response: Response;
	try {
		({ data, response } = await api.PUT('/api/v1/playlists/{playlist_id}/tracks', {
			params: { path: { playlist_id: playlistId }, header: { 'If-Match': etag } },
			body: { stable_ids: stableIds }
		}));
	} catch (error) {
		_throwWriteError(error, `update playlist ${playlistId}`);
	}
	const fresh = response.headers.get('etag');
	if (!fresh) throw new Error(`playlist ${playlistId}: PUT response carries no ETag header`);
	const out = data as PlaylistRowWire;
	return { items: out.items, etag: fresh };
}

/** POST /playlists - create empty playlist (201 + ETag). No If-Match, so
 * unlike the mutations below a failure never maps to PlaylistConflictError
 * (exactly as before the conversion). */
export async function createPlaylist(name: string): Promise<PlaylistRowWire> {
	try {
		return await unwrap(api.POST('/api/v1/playlists', { body: { name } }));
	} catch (error) {
		if (error instanceof ApiError) {
			throw new Error(`create playlist failed (${error.status}): ${_messageOf(error)}`);
		}
		throw error;
	}
}

/** PATCH /playlists/{id} - rename (If-Match required). */
export async function renamePlaylist(
	playlistId: string,
	etag: string,
	name: string
): Promise<PlaylistRowWire> {
	try {
		return await unwrap(
			api.PATCH('/api/v1/playlists/{playlist_id}', {
				params: { path: { playlist_id: playlistId }, header: { 'If-Match': etag } },
				body: { name }
			})
		);
	} catch (error) {
		_throwWriteError(error, `rename playlist ${playlistId}`);
	}
}

/** DELETE /playlists/{id} (If-Match required) -> 204, so no unwrap: the
 * client's middleware has already thrown on any non-2xx. */
export async function deletePlaylist(playlistId: string, etag: string): Promise<void> {
	try {
		await api.DELETE('/api/v1/playlists/{playlist_id}', {
			params: { path: { playlist_id: playlistId }, header: { 'If-Match': etag } }
		});
	} catch (error) {
		_throwWriteError(error, `delete playlist ${playlistId}`);
	}
}
