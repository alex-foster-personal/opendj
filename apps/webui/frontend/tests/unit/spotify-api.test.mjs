import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://spotify-api.example.test';
const PLAYLIST_ID = 'spotify:playlist/one';
const SOURCES = {
	beatport: 'https://www.beatport.com/search?q=artist+title',
	bandcamp: 'https://bandcamp.com/search?q=artist+title&item_type=t',
	qobuz: 'https://www.qobuz.com/us-en/search?q=artist+title',
	apple_music: 'https://music.apple.com/us/search?term=artist+title',
	discogs: 'https://www.discogs.com/search?q=artist+title&type=release'
};

let api;
let originalFetch;

before(async () => {
	api = await loadTypeScriptModule('src/lib/rb/spotify-api.ts', { viteApiBase: API_BASE });
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('requests the exact playlist buy-list URL and preserves persisted source links', async () => {
	let requestedUrl = '';
	globalThis.fetch = async (input) => {
		requestedUrl = String(input);
		return Response.json([
			{
				pending_id: 17,
				playlist_id: PLAYLIST_ID,
				position: 3,
				spotify_uri: 'spotify:track:abc',
				isrc: null,
				title: 'Track title',
				artist: 'Artist name',
				album: 'Album name',
				duration_ms: 241000,
				suggested_sources: SOURCES,
				status: 'pending',
				added_at: '2026-07-22T12:00:00+00:00'
			}
		]);
	};

	const pending = await api.getSpotifyPendingTracks(PLAYLIST_ID);

	assert.equal(
		requestedUrl,
		`${API_BASE}/api/v1/spotify/playlists/spotify%3Aplaylist%2Fone/pending-tracks`
	);
	assert.deepEqual(pending[0].suggested_sources, SOURCES);
});

test('fails loudly when a canonical purchase link is missing', async () => {
	globalThis.fetch = async () =>
		Response.json([
			{
				pending_id: 17,
				playlist_id: PLAYLIST_ID,
				position: 3,
				spotify_uri: 'spotify:track:abc',
				isrc: null,
				title: 'Track title',
				artist: 'Artist name',
				album: 'Album name',
				duration_ms: 241000,
				suggested_sources: { ...SOURCES, discogs: undefined },
				status: 'pending',
				added_at: '2026-07-22T12:00:00+00:00'
			}
		]);

	await assert.rejects(
		() => api.getSpotifyPendingTracks(PLAYLIST_ID),
		/canonical suggested_sources/
	);
});

test('surfaces an explicit HTTP failure', async () => {
	globalThis.fetch = async () => Response.json({ detail: 'not an imported Spotify playlist' }, { status: 404 });

	await assert.rejects(
		() => api.getSpotifyPendingTracks(PLAYLIST_ID),
		/error 404/i
	);
});
