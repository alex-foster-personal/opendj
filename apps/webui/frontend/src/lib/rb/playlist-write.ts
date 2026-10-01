/**
 * Playlist write lane: the mutation client plus the blank-playlist policy
 * that decides which of those mutations to issue.
 *
 * Two halves, one concern, one owner:
 *   1. TRANSPORT - create / rename / delete / replace-membership against the
 *      write router, with If-Match optimistic concurrency.
 *   2. POLICY (pure, no I/O) - blank-playlist identity and the create-grace
 *      registry behind the auto-delete sweep. It exists only to choose the
 *      `deletePlaylist` calls the transport half then makes, so splitting it
 *      into its own module bought nothing but an extra edge into
 *      BrowserPanel.svelte (the single consumer of both halves).
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

/** One membership row an items:add inserted (MembershipAddedOut). */
export interface PlaylistAddedMember {
	item_id: string;
	stable_id: string;
	order_key: string;
}

/** items:add answers the inserted rows, never the whole membership
 * (LIBM-132, #3963): at 10k members the full list was the add's cost. */
export interface PlaylistAddResult {
	added: PlaylistAddedMember[];
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

/** POST /playlists/{id}/items:add - O(1) append/insert without rewriting
 * existing membership rows (LIBM-20). No If-Match required. Returns the
 * inserted rows only (LIBM-132); refetch the playlist for its membership. */
export async function addPlaylistItems(
	playlistId: string,
	stableIds: string[],
	position?: number
): Promise<PlaylistAddResult> {
	let data: unknown;
	let response: Response;
	const body: { stable_ids: string[]; position?: number } = { stable_ids: stableIds };
	if (position !== undefined) {
		body.position = position;
	}
	try {
		({ data, response } = await api.POST('/api/v1/playlists/{playlist_id}/items:add', {
			params: { path: { playlist_id: playlistId } },
			body
		}));
	} catch (error) {
		if (error instanceof ApiError) {
			throw new Error(
				`add to playlist ${playlistId} failed (${error.status}): ${_messageOf(error)}`
			);
		}
		throw error;
	}
	const fresh = response.headers.get('etag');
	if (!fresh) {
		throw new Error(`playlist ${playlistId}: POST items:add response carries no ETag header`);
	}
	const added = (data as { added?: PlaylistAddedMember[] } | undefined)?.added;
	if (!Array.isArray(added)) {
		throw new Error(`playlist ${playlistId}: POST items:add response carries no added rows`);
	}
	return { added, etag: fresh };
}

/** DELETE /playlists/{id}/items/{item_id} - O(1) remove without rewriting
 * existing membership rows (LIBM-21). No If-Match required. */
export async function deletePlaylistItem(
	playlistId: string,
	itemId: string
): Promise<PlaylistWriteResult> {
	let data: unknown;
	let response: Response;
	try {
		({ data, response } = await api.DELETE('/api/v1/playlists/{playlist_id}/items/{item_id}', {
			params: { path: { playlist_id: playlistId, item_id: itemId } }
		}));
	} catch (error) {
		if (error instanceof ApiError) {
			throw new Error(
				`remove from playlist ${playlistId} failed (${error.status}): ${_messageOf(error)}`
			);
		}
		throw error;
	}
	const fresh = response.headers.get('etag');
	if (!fresh) {
		throw new Error(
			`playlist ${playlistId}: DELETE item response carries no ETag header`
		);
	}
	const out = data as PlaylistRowWire;
	return { items: out.items, etag: fresh };
}

/** POST /playlists/{id}/items:remove - O(k) bulk remove without rewriting
 * existing membership rows (LIBM-21). No If-Match required. */
export async function removePlaylistItems(
	playlistId: string,
	itemIds: string[]
): Promise<PlaylistWriteResult> {
	let data: unknown;
	let response: Response;
	try {
		({ data, response } = await api.POST('/api/v1/playlists/{playlist_id}/items:remove', {
			params: { path: { playlist_id: playlistId } },
			body: { item_ids: itemIds }
		}));
	} catch (error) {
		if (error instanceof ApiError) {
			throw new Error(
				`remove from playlist ${playlistId} failed (${error.status}): ${_messageOf(error)}`
			);
		}
		throw error;
	}
	const fresh = response.headers.get('etag');
	if (!fresh) {
		throw new Error(
			`playlist ${playlistId}: POST items:remove response carries no ETag header`
		);
	}
	const out = data as PlaylistRowWire;
	return { items: out.items, etag: fresh };
}

export interface MembershipMoveBody {
	range_start: string;
	range_length?: number;
	range_end?: string;
	before_item_id?: string;
	after_item_id?: string;
}

/** POST /playlists/{id}/items:move - O(k) contiguous slice reorder
 * without rewriting neighbors (LIBM-22). Throws PlaylistConflictError on
 * a stale If-Match (409). */
export async function movePlaylistItems(
	playlistId: string,
	etag: string,
	body: MembershipMoveBody
): Promise<PlaylistWriteResult & { renumbered?: boolean }> {
	let data: unknown;
	let response: Response;
	try {
		({ data, response } = await api.POST('/api/v1/playlists/{playlist_id}/items:move', {
			params: { path: { playlist_id: playlistId }, header: { 'If-Match': etag } },
			body
		}));
	} catch (error) {
		_throwWriteError(error, `move playlist ${playlistId}`);
	}
	const fresh = response.headers.get('etag');
	if (!fresh) {
		throw new Error(
			`playlist ${playlistId}: POST items:move response carries no ETag header`
		);
	}
	const out = data as PlaylistRowWire & { renumbered?: boolean };
	return {
		items: out.items,
		etag: fresh,
		...(out.renumbered === undefined ? {} : { renumbered: out.renumbered })
	};
}

/** POST /playlists/{id}/tracks/transfer - atomic cross-playlist add (copy)
 * or move. Throws PlaylistConflictError on a stale If-Match (409). */
export async function transferPlaylistTracks(
	destId: string,
	destEtag: string,
	body: {
		stable_ids: string[];
		mode: 'add' | 'move';
		source_playlist_id?: string;
		source_etag?: string;
	}
): Promise<{ dest: PlaylistRowWire; source: PlaylistRowWire | null; etag: string }> {
	let data: unknown;
	let response: Response;
	try {
		({ data, response } = await api.POST('/api/v1/playlists/{playlist_id}/tracks/transfer', {
			params: { path: { playlist_id: destId }, header: { 'If-Match': destEtag } },
			body
		}));
	} catch (error) {
		_throwWriteError(error, `transfer tracks to playlist ${destId}`);
	}
	const fresh = response.headers.get('etag');
	if (!fresh) {
		throw new Error(`playlist ${destId}: POST transfer response carries no ETag header`);
	}
	const out = data as { dest: PlaylistRowWire; source: PlaylistRowWire | null };
	return { dest: out.dest, source: out.source ?? null, etag: fresh };
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

export type PlaylistPatchBody = {
	name?: string;
	forbid_duplicates?: boolean;
};

/** PATCH /playlists/{id} - rename and/or forbid_duplicates (If-Match required). */
export async function patchPlaylist(
	playlistId: string,
	etag: string,
	body: PlaylistPatchBody
): Promise<PlaylistRowWire> {
	try {
		return await unwrap(
			api.PATCH('/api/v1/playlists/{playlist_id}', {
				params: { path: { playlist_id: playlistId }, header: { 'If-Match': etag } },
				body
			})
		);
	} catch (error) {
		_throwWriteError(error, `patch playlist ${playlistId}`);
	}
}

/** PATCH /playlists/{id} - rename (If-Match required). */
export async function renamePlaylist(
	playlistId: string,
	etag: string,
	name: string
): Promise<PlaylistRowWire> {
	return patchPlaylist(playlistId, etag, { name });
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

/** POST /playlists/{id}/duplicate - copy playlist with optional name override.
 * Optional If-Match is CAS on the source row. Omit body to use the server
 * default "<source name> (copy)". */
export async function duplicatePlaylist(
	playlistId: string,
	etag?: string,
	name?: string
): Promise<PlaylistRowWire> {
	try {
		const params: {
			path: { playlist_id: string };
			header?: { 'If-Match': string };
		} = { path: { playlist_id: playlistId } };
		if (etag !== undefined) {
			params.header = { 'If-Match': etag };
		}
		const options: {
			params: typeof params;
			body?: { name: string };
		} = { params };
		if (name !== undefined) {
			options.body = { name };
		}
		return await unwrap(
			api.POST('/api/v1/playlists/{playlist_id}/duplicate', options)
		);
	} catch (error) {
		_throwWriteError(error, `duplicate playlist ${playlistId}`);
	}
}

// ----------------------------------------------------------------------
// Blank playlist policy (pure - no I/O, no api client)
//
// Blank = empty membership AND name still a create-default / untitled
// sentinel (case-insensitive trim). A user-renamed empty playlist must
// survive until they delete it. Newly created blanks get a short grace
// window so in-place rename after '+' can finish.
// ----------------------------------------------------------------------

export const DEFAULT_PLAYLIST_NAME = 'New Playlist';
export const BLANK_PLAYLIST_GRACE_MS = 60_000;

const LOG_PREFIX = '[playlist-blank]';

/** Case-folded create-default / untitled sentinels. */
const BLANK_NAME_SENTINELS = new Set([
	'new playlist',
	'untitled',
	'untitled playlist',
	''
]);

/** playlist_id -> grace deadline (epoch ms). */
const _graceUntil = new Map<string, number>();

export type BlankPlaylistLike = {
	playlist_id: string;
	name: string;
	track_count: number;
	/** When set, only `webui` rows are delete candidates. */
	vendor?: string;
	/** Server last-touch timestamp (ISO 8601). Backs the grace check across a
	 * reload, where `_graceUntil` (in-memory, this module) does not survive. */
	updated_at?: string;
};

export type BlankDeleteDecision =
	| { action: 'delete'; reason: string }
	| { action: 'keep'; reason: string };

export function isBlankPlaylistName(name: string): boolean {
	return BLANK_NAME_SENTINELS.has(name.trim().toLowerCase());
}

/** Empty membership + still default/untitled identity. */
export function isBlankPlaylist(p: BlankPlaylistLike): boolean {
	return p.track_count === 0 && isBlankPlaylistName(p.name);
}

export function markPlaylistCreateGrace(playlistId: string, nowMs: number = Date.now()): void {
	_graceUntil.set(playlistId, nowMs + BLANK_PLAYLIST_GRACE_MS);
}

export function clearPlaylistCreateGrace(playlistId: string): void {
	_graceUntil.delete(playlistId);
}

export function isWithinCreateGrace(playlistId: string, nowMs: number = Date.now()): boolean {
	const until = _graceUntil.get(playlistId);
	if (until === undefined) return false;
	if (nowMs >= until) {
		_graceUntil.delete(playlistId);
		return false;
	}
	return true;
}

/** Test-only: clear in-memory grace registry. */
export function _resetCreateGraceForTests(): void {
	_graceUntil.clear();
}

/** Server-timestamp fallback for `isWithinCreateGrace`: a row this recently
 * touched is presumed still mid-create even with no (or an evicted)
 * `_graceUntil` entry -- the case a reload produces, since that registry is
 * in-memory only and does not survive one. */
function isWithinServerGrace(updatedAt: string | undefined, nowMs: number): boolean {
	if (updatedAt === undefined) return false;
	const updatedMs = Date.parse(updatedAt);
	return Number.isFinite(updatedMs) && nowMs - updatedMs < BLANK_PLAYLIST_GRACE_MS;
}

export function decideBlankPlaylistDelete(
	p: BlankPlaylistLike,
	nowMs: number = Date.now()
): BlankDeleteDecision {
	if (p.vendor !== undefined && p.vendor !== 'webui') {
		return { action: 'keep', reason: `vendor=${p.vendor} (not webui)` };
	}
	if (p.track_count > 0) {
		return { action: 'keep', reason: `has ${p.track_count} track(s)` };
	}
	if (!isBlankPlaylistName(p.name)) {
		return { action: 'keep', reason: `renamed name=${JSON.stringify(p.name.trim())}` };
	}
	if (
		isWithinCreateGrace(p.playlist_id, nowMs) ||
		isWithinServerGrace(p.updated_at, nowMs)
	) {
		return { action: 'keep', reason: `within ${BLANK_PLAYLIST_GRACE_MS}ms create grace` };
	}
	return {
		action: 'delete',
		reason: 'blank untitled empty (grace elapsed or absent)'
	};
}

export type BlankSweepLog = (message: string) => void;

/**
 * Decide which empty playlists to auto-delete. Logs every empty-row
 * decision (delete or keep-with-reason). Non-empty rows are never
 * candidates and are not logged.
 */
export function collectBlankPlaylistDeletes(
	playlists: readonly BlankPlaylistLike[],
	nowMs: number = Date.now(),
	log: BlankSweepLog = (msg) => console.info(msg)
): string[] {
	const ids: string[] = [];
	for (const p of playlists) {
		if (p.track_count > 0) continue;
		const d = decideBlankPlaylistDelete(p, nowMs);
		if (d.action === 'delete') {
			log(`${LOG_PREFIX} delete ${p.playlist_id}: ${d.reason}`);
			ids.push(p.playlist_id);
		} else {
			log(`${LOG_PREFIX} keep ${p.playlist_id}: ${d.reason}`);
		}
	}
	return ids;
}
