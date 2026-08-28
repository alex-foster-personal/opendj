/**
 * Read-only contract for the Spotify source panel's persisted acquisition
 * queue. Source links are returned verbatim from the backend - this client
 * never derives a purchase URL or infers a missing track from playlist counts.
 */

import { ApiError, api, unwrap } from '$lib/api/client';

export const SPOTIFY_PURCHASE_SOURCES = [
	'beatport',
	'bandcamp',
	'qobuz',
	'apple_music',
	'discogs'
] as const;

export type SpotifyPurchaseSource = (typeof SPOTIFY_PURCHASE_SOURCES)[number];

export type SpotifyPurchaseLinks = Record<SpotifyPurchaseSource, string>;

export interface SpotifyPendingTrack {
	pending_id: number;
	playlist_id: string;
	position: number;
	spotify_uri: string;
	isrc: string | null;
	title: string;
	artist: string;
	album: string | null;
	duration_ms: number | null;
	suggested_sources: SpotifyPurchaseLinks;
	status: 'pending';
	added_at: string;
}

function _isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function _expectString(value: unknown, field: string): string {
	if (typeof value !== 'string') throw new Error(`Spotify pending-track contract violation: ${field}`);
	return value;
}

function _expectNullableString(value: unknown, field: string): string | null {
	if (value !== null && typeof value !== 'string') {
		throw new Error(`Spotify pending-track contract violation: ${field}`);
	}
	return value;
}

function _expectNumber(value: unknown, field: string): number {
	if (typeof value !== 'number' || !Number.isFinite(value)) {
		throw new Error(`Spotify pending-track contract violation: ${field}`);
	}
	return value;
}

function _purchaseLinks(value: unknown): SpotifyPurchaseLinks {
	if (!_isRecord(value)) {
		throw new Error('Spotify pending-track contract violation: suggested_sources');
	}
	const keys = Object.keys(value).sort();
	const expected = [...SPOTIFY_PURCHASE_SOURCES].sort();
	if (keys.length !== expected.length || keys.some((key, index) => key !== expected[index])) {
		throw new Error('Spotify pending-track contract violation: canonical suggested_sources keys');
	}
	const links = {} as SpotifyPurchaseLinks;
	for (const source of SPOTIFY_PURCHASE_SOURCES) {
		links[source] = _expectString(value[source], `canonical suggested_sources.${source}`);
	}
	return links;
}

function _pendingTrack(value: unknown, index: number): SpotifyPendingTrack {
	if (!_isRecord(value)) throw new Error(`Spotify pending-track contract violation: row ${index}`);
	const status = _expectString(value.status, `row ${index}.status`);
	if (status !== 'pending') {
		throw new Error(`Spotify pending-track contract violation: row ${index}.status must be pending`);
	}
	return {
		pending_id: _expectNumber(value.pending_id, `row ${index}.pending_id`),
		playlist_id: _expectString(value.playlist_id, `row ${index}.playlist_id`),
		position: _expectNumber(value.position, `row ${index}.position`),
		spotify_uri: _expectString(value.spotify_uri, `row ${index}.spotify_uri`),
		isrc: _expectNullableString(value.isrc, `row ${index}.isrc`),
		title: _expectString(value.title, `row ${index}.title`),
		artist: _expectString(value.artist, `row ${index}.artist`),
		album: _expectNullableString(value.album, `row ${index}.album`),
		duration_ms:
			value.duration_ms === null
				? null
				: _expectNumber(value.duration_ms, `row ${index}.duration_ms`),
		suggested_sources: _purchaseLinks(value.suggested_sources),
		status: 'pending',
		added_at: _expectString(value.added_at, `row ${index}.added_at`)
	};
}

/** GET the persisted, ordered acquisition rows for one imported Spotify playlist. */
export async function getSpotifyPendingTracks(playlistId: string): Promise<SpotifyPendingTrack[]> {
	if (playlistId.trim() === '') throw new Error('Spotify playlist id must not be empty');
	let body: unknown;
	try {
		body = await unwrap(
			api.GET('/api/v1/spotify/playlists/{playlist_id}/pending-tracks', {
				params: { path: { playlist_id: playlistId } }
			})
		);
	} catch (error) {
		if (error instanceof ApiError) {
			throw new Error(
				`Spotify pending tracks request failed with error ${error.status}: ${error.response.statusText}`
			);
		}
		throw error;
	}
	if (!Array.isArray(body)) {
		throw new Error('Spotify pending-track contract violation: expected a top-level list');
	}
	return body.map(_pendingTrack);
}
