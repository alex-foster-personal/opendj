/**
 * The signed-in Spotify account's playlists and the in-app Connect flow
 * (SPOT-6555). Kept apart from spotify-api.ts so only the lazily loaded Spotify
 * panel pays for it, not the library first paint.
 */

import { API_BASE } from '$lib/api/base';
import { api, unwrap } from './spotify-api';

// Pure copy and checks live apart so a props-only view can import them
// without reaching the backend (storybook-stories.test.mjs).
export { isSpotifyClientId, spotifyNotConnectedText } from './spotify-account-copy';

function _isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function _expectString(value: unknown, field: string): string {
	if (typeof value !== 'string') throw new Error(`Spotify account contract violation: ${field}`);
	return value;
}

function _expectNullableString(value: unknown, field: string): string | null {
	if (value !== null && typeof value !== 'string') {
		throw new Error(`Spotify account contract violation: ${field}`);
	}
	return value;
}

function _expectBoolean(value: unknown, field: string): boolean {
	if (typeof value !== 'boolean') throw new Error(`Spotify account contract violation: ${field}`);
	return value;
}

function _expectNonEmptyString(value: unknown, field: string): string {
	if (typeof value !== 'string' || value === '')
		throw new Error(`Spotify account contract violation: ${field}`);
	return value;
}

function _expectCount(value: unknown, field: string): number {
	if (!(typeof value === 'number' && Number.isInteger(value) && value >= 0)) {
		throw new Error(`Spotify account contract violation: ${field}`);
	}
	return value;
}

function _expectNullableBoolean(value: unknown, field: string): boolean | null {
	if (value !== null && typeof value !== 'boolean')
		throw new Error(`Spotify account contract violation: ${field}`);
	return value;
}

function _expectNullableCount(value: unknown, field: string): number | null {
	if (value !== null && !(typeof value === 'number' && Number.isInteger(value) && value >= 0)) {
		throw new Error(`Spotify account contract violation: ${field}`);
	}
	return value;
}

/**
 * Every field is checked, and a nullable one must be present as null: a
 * missing or mistyped field is a contract break, never a default. Ids are
 * nonempty and the owner is required, as the engine requires them.
 */
function _parsePlaylist(value: unknown, index: number): SpotifyAccountPlaylist {
	const at = (field: string): string => `playlists[${index}].${field}`;
	if (!_isRecord(value)) throw new Error(`Spotify account contract violation: playlists[${index}]`);
	// The id is base-62 and the player URL is the one built from it, as the
	// engine guarantees, so a one-sided change cannot open another playlist.
	const spotifyId = _expectNonEmptyString(value.spotify_id, at('spotify_id'));
	if (!/^[A-Za-z0-9]+$/.test(spotifyId))
		throw new Error(`Spotify account contract violation: ${at('spotify_id')}`);
	if (value.open_url !== `https://open.spotify.com/playlist/${spotifyId}`) {
		throw new Error(`Spotify account contract violation: ${at('open_url')}`);
	}
	return {
		spotify_id: spotifyId,
		name: _expectString(value.name, at('name')),
		owner_id: _expectNonEmptyString(value.owner_id, at('owner_id')),
		owner_name: _expectNullableString(value.owner_name, at('owner_name')),
		owned: _expectBoolean(value.owned, at('owned')),
		collaborative: _expectBoolean(value.collaborative, at('collaborative')),
		public: _expectNullableBoolean(value.public, at('public')),
		track_count: _expectNullableCount(value.track_count, at('track_count')),
		image_url: _expectNullableString(value.image_url, at('image_url')),
		open_url: value.open_url,
		snapshot_id: _expectNullableString(value.snapshot_id, at('snapshot_id'))
	};
}

const NOT_CONNECTED_REASONS: ReadonlySet<string> = new Set([
	'spotify_extra_missing',
	'no_client_id',
	'not_signed_in',
	'scope_missing',
	'token_refresh_failed'
]);

/** One playlist on the signed-in account, imported into Open DJ or not (SPOT-6555). */
export interface SpotifyAccountPlaylist {
	spotify_id: string;
	name: string;
	owner_id: string;
	owner_name: string | null;
	owned: boolean;
	collaborative: boolean;
	public: boolean | null;
	track_count: number | null;
	image_url: string | null;
	open_url: string;
	snapshot_id: string | null;
}

export type SpotifyNotConnectedReason =
	| 'spotify_extra_missing'
	| 'no_client_id'
	| 'not_signed_in'
	| 'scope_missing'
	| 'token_refresh_failed';

export type SpotifyAccountListing =
	| {
			connected: true;
			user_id: string;
			display_name: string | null;
			total_reported: number;
			playlists: SpotifyAccountPlaylist[];
	  }
	| { connected: false; reason: SpotifyNotConnectedReason; message: string; playlists: [] };

/** GET every playlist the signed-in account owns or follows, all pages. */
export async function getSpotifyAccountPlaylists(): Promise<SpotifyAccountListing> {
	return parseSpotifyAccountListing(await unwrap(api.GET('/api/v1/spotify/account/playlists')));
}

/** Check the engine's listing body field by field; any break throws. */
export function parseSpotifyAccountListing(body: unknown): SpotifyAccountListing {
	if (!_isRecord(body) || typeof body.connected !== 'boolean') {
		throw new Error('Spotify account contract violation: connected');
	}
	if (!body.connected) {
		const reason = _expectString(body.reason, 'reason');
		if (!NOT_CONNECTED_REASONS.has(reason))
			throw new Error('Spotify account contract violation: reason');
		// The engine always sends playlists, empty when disconnected.
		if (!Array.isArray(body.playlists) || body.playlists.length !== 0) {
			throw new Error('Spotify account contract violation: playlists');
		}
		return {
			connected: false,
			reason: reason as SpotifyNotConnectedReason,
			message: _expectString(body.message, 'message'),
			playlists: []
		};
	}
	if (!Array.isArray(body.playlists)) {
		throw new Error('Spotify account contract violation: playlists');
	}
	return {
		connected: true,
		user_id: _expectNonEmptyString(body.user_id, 'user_id'),
		display_name: _expectNullableString(body.display_name, 'display_name'),
		total_reported: _expectCount(body.total_reported, 'total_reported'),
		playlists: body.playlists.map(_parsePlaylist)
	};
}

const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '[::1]', '::1']);

/**
 * Whether Connect can finish here. Spotify sends the browser back to
 * http://127.0.0.1:8888/callback, which the ENGINE listens on, so the
 * browser and the engine must be the same machine. With the engine reached
 * over the network (agentbox, a remote daemon) the redirect lands on the
 * browser's own loopback and the sign-in can never complete.
 */
export function spotifyConnectRunsHere(
	apiBase: string,
	pageHostname: string,
	nativeShell: boolean
): boolean {
	const engineHost = apiBase === '' ? null : new URL(apiBase, 'http://127.0.0.1').hostname;
	// A desktop shell hands the sign-in to the system browser on its own
	// machine, so only the engine's address matters there.
	if (nativeShell) return engineHost === null || LOOPBACK_HOSTS.has(engineHost);
	return LOOPBACK_HOSTS.has(engineHost ?? pageHostname) && LOOPBACK_HOSTS.has(pageHostname);
}

/** {@link spotifyConnectRunsHere} for this page. */
export function spotifyConnectRunsOnThisPage(nativeShell: boolean): boolean {
	return spotifyConnectRunsHere(API_BASE, globalThis.location?.hostname ?? '', nativeShell);
}

/**
 * Save the Client ID a fresh install needs before Connect can start. It is a
 * public PKCE id naming the user's own Spotify app, not a secret.
 */
export async function saveSpotifyClientId(clientId: string): Promise<void> {
	await unwrap(
		api.PUT('/api/v1/spotify/account/client-id', { body: { client_id: clientId.trim() } })
	);
}

/** Start the in-app sign-in; returns the URL to open in a real browser. */
export async function startSpotifyConnect(): Promise<string> {
	const body = await unwrap(api.POST('/api/v1/spotify/account/connect'));
	return _expectString((body as { authorize_url?: unknown }).authorize_url, 'authorize_url');
}

export type SpotifyConnectPhase = 'idle' | 'waiting' | 'connected' | 'failed';

export async function getSpotifyConnectStatus(): Promise<{
	phase: SpotifyConnectPhase;
	detail: string | null;
}> {
	const body = await unwrap(api.GET('/api/v1/spotify/account/connect'));
	return body as { phase: SpotifyConnectPhase; detail: string | null };
}
