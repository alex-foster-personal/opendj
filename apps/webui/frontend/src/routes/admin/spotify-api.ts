/**
 * Route-local clients for the two Spotify admin panes.
 *
 * GET /api/v1/spotify/status        - can the daemon actually reach Spotify
 * GET /api/v1/spotify/recently-played - live plays, scored against the library
 *
 * Both responses are structurally validated before render, in the same
 * fail-loud style as `kpi-api.ts` and `$lib/rb/spotify-api.ts`: a wrong type
 * anywhere is an explicit error rather than a blank cell. Purchase URLs are
 * returned verbatim by the backend and never derived here, so the UI and the
 * CLI open byte-identical links.
 */

import { ApiError, api, unwrap } from '$lib/api/client';
import {
	SPOTIFY_PURCHASE_SOURCES,
	type SpotifyPurchaseLinks,
	type SpotifyPurchaseSource
} from '$lib/rb/spotify-api';

export { SPOTIFY_PURCHASE_SOURCES };
export type { SpotifyPurchaseLinks, SpotifyPurchaseSource };

/** Human label per store key, for the buy buttons. */
export const PURCHASE_SOURCE_LABELS: Record<SpotifyPurchaseSource, string> = {
	beatport: 'Beatport',
	bandcamp: 'Bandcamp',
	qobuz: 'Qobuz',
	apple_music: 'Apple Music',
	discogs: 'Discogs'
};

export type SpotifyOwnership = 'owned' | 'review' | 'unmatched';

export interface SpotifyStatus {
	connected: boolean;
	needs_authorization: boolean;
	client_id_present: boolean;
	token_cache_path: string;
	token_cache_present: boolean;
	scopes: string[];
	display_name: string | null;
	account_id: string | null;
	product: string | null;
	error: string | null;
}

export interface SpotifyRecentPlay {
	played_at: string;
	spotify_uri: string;
	isrc: string | null;
	title: string;
	artist: string;
	album: string | null;
	duration_ms: number | null;
	ownership: SpotifyOwnership;
	confidence: number;
	matched_stable_id: string | null;
	suggested_sources: SpotifyPurchaseLinks;
}

//----- validators -----------------------------------------------------------

function _asObject(value: unknown, ctx: string): Record<string, unknown> {
	if (typeof value !== 'object' || value === null || Array.isArray(value)) {
		throw new Error(`spotify admin: ${ctx} is not an object`);
	}
	return value as Record<string, unknown>;
}

function _asString(value: unknown, ctx: string): string {
	if (typeof value !== 'string') throw new Error(`spotify admin: ${ctx} is not a string`);
	return value;
}

function _asNullableString(value: unknown, ctx: string): string | null {
	if (value !== null && typeof value !== 'string') {
		throw new Error(`spotify admin: ${ctx} is not a string or null`);
	}
	return value;
}

function _asBoolean(value: unknown, ctx: string): boolean {
	if (typeof value !== 'boolean') throw new Error(`spotify admin: ${ctx} is not a boolean`);
	return value;
}

function _asNumber(value: unknown, ctx: string): number {
	if (typeof value !== 'number' || !Number.isFinite(value)) {
		throw new Error(`spotify admin: ${ctx} is not a finite number`);
	}
	return value;
}

function _asPurchaseLinks(value: unknown, ctx: string): SpotifyPurchaseLinks {
	const obj = _asObject(value, ctx);
	const links = {} as SpotifyPurchaseLinks;
	for (const source of SPOTIFY_PURCHASE_SOURCES) {
		links[source] = _asString(obj[source], `${ctx}.${source}`);
	}
	return links;
}

function _asOwnership(value: unknown, ctx: string): SpotifyOwnership {
	const raw = _asString(value, ctx);
	if (raw !== 'owned' && raw !== 'review' && raw !== 'unmatched') {
		throw new Error(`spotify admin: ${ctx} must be owned/review/unmatched, got '${raw}'`);
	}
	return raw;
}

function _rethrow(error: unknown, what: string): never {
	if (error instanceof ApiError) {
		throw new Error(`${what} failed with error ${error.status}: ${error.message}`);
	}
	throw error;
}

//----- calls ----------------------------------------------------------------

/** Probe the daemon's Spotify connection. `connected` is only ever true after
 * the backend completed a live profile call, so a stale token reads false. */
export async function fetchSpotifyStatus(): Promise<SpotifyStatus> {
	let body: unknown;
	try {
		body = await unwrap(api.GET('/api/v1/spotify/status'));
	} catch (error) {
		_rethrow(error, 'Spotify status');
	}
	const obj = _asObject(body, 'status');
	const scopes = obj.scopes;
	if (!Array.isArray(scopes)) throw new Error('spotify admin: status.scopes is not a list');
	return {
		connected: _asBoolean(obj.connected, 'status.connected'),
		needs_authorization: _asBoolean(obj.needs_authorization, 'status.needs_authorization'),
		client_id_present: _asBoolean(obj.client_id_present, 'status.client_id_present'),
		token_cache_path: _asString(obj.token_cache_path, 'status.token_cache_path'),
		token_cache_present: _asBoolean(obj.token_cache_present, 'status.token_cache_present'),
		scopes: scopes.map((scope, index) => _asString(scope, `status.scopes[${index}]`)),
		display_name: _asNullableString(obj.display_name, 'status.display_name'),
		account_id: _asNullableString(obj.account_id, 'status.account_id'),
		product: _asNullableString(obj.product, 'status.product'),
		error: _asNullableString(obj.error, 'status.error')
	};
}

/** Live plays, newest first. An empty list means Spotify reported no plays;
 * a failure throws, so "nothing recent" can never be a broken call's output. */
export async function fetchRecentPlays(limit: number): Promise<SpotifyRecentPlay[]> {
	if (!Number.isInteger(limit) || limit < 1 || limit > 50) {
		throw new Error(`spotify admin: limit must be an integer 1..50, got ${limit}`);
	}
	let body: unknown;
	try {
		body = await unwrap(
			api.GET('/api/v1/spotify/recently-played', { params: { query: { limit } } })
		);
	} catch (error) {
		_rethrow(error, 'Spotify listen history');
	}
	if (!Array.isArray(body)) {
		throw new Error('spotify admin: recently-played did not return a list');
	}
	return body.map((raw, index) => {
		const obj = _asObject(raw, `play ${index}`);
		return {
			played_at: _asString(obj.played_at, `play ${index}.played_at`),
			spotify_uri: _asString(obj.spotify_uri, `play ${index}.spotify_uri`),
			isrc: _asNullableString(obj.isrc, `play ${index}.isrc`),
			title: _asString(obj.title, `play ${index}.title`),
			artist: _asString(obj.artist, `play ${index}.artist`),
			album: _asNullableString(obj.album, `play ${index}.album`),
			duration_ms:
				obj.duration_ms === null ? null : _asNumber(obj.duration_ms, `play ${index}.duration_ms`),
			ownership: _asOwnership(obj.ownership, `play ${index}.ownership`),
			confidence: _asNumber(obj.confidence, `play ${index}.confidence`),
			matched_stable_id: _asNullableString(
				obj.matched_stable_id,
				`play ${index}.matched_stable_id`
			),
			suggested_sources: _asPurchaseLinks(obj.suggested_sources, `play ${index}.suggested_sources`)
		};
	});
}

/** The one command that completes the Spotify browser sign-in.
 *
 * The daemon deliberately cannot run this. PKCE opens a browser and waits on a
 * loopback callback, which only works where a human is sitting; a server doing
 * it would open a browser nobody can see and block the request. So the UI's
 * honest affordance is this command, shown verbatim to be copied, not a button
 * that pretends to log you in.
 */
export const SPOTIFY_AUTH_COMMAND =
	'doppler run -p construct -c dev_af -- python -m apps.spotify recent --limit 1';
