/**
 * Wire-shape regression for the track playlist reverse lookup client.
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://track-playlists.example.test';

let trackPlaylists;
let readApiErrorStatus;
let originalFetch;

function jsonResponse(body, init = {}) {
	return new Response(JSON.stringify(body), {
		status: 200,
		...init,
		headers: { 'content-type': 'application/json', ...(init.headers ?? {}) }
	});
}

before(async () => {
	const client = await loadTypeScriptModule('src/lib/api/client.ts', {
		viteApiBase: API_BASE
	});
	readApiErrorStatus = client.readApiErrorStatus;
	trackPlaylists = await loadTypeScriptModule('src/lib/rb/track-playlists.ts', {
		viteApiBase: API_BASE
	});
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('listTrackPlaylists GETs the reverse lookup path and round-trips a 200 array', async () => {
	let seen;
	globalThis.fetch = async (request) => {
		seen = request;
		return jsonResponse([
			{
				playlist_id: 'pl-1',
				name: 'Peak Hour',
				vendor: 'webui',
				positions: [0, 4]
			}
		]);
	};

	const hits = await trackPlaylists.listTrackPlaylists('track-abc');

	assert.equal(seen.url, `${API_BASE}/api/v1/tracks/track-abc/playlists`);
	assert.equal(seen.method, 'GET');
	assert.deepEqual(hits, [
		{
			playlist_id: 'pl-1',
			name: 'Peak Hour',
			vendor: 'webui',
			positions: [0, 4]
		}
	]);
});

test('listTrackPlaylists throws on 404 instead of returning an empty array', async () => {
	globalThis.fetch = async () =>
		new Response(JSON.stringify({ detail: { code: 'not_found', message: 'missing' } }), {
			status: 404,
			headers: { 'content-type': 'application/json' }
		});

	await assert.rejects(trackPlaylists.listTrackPlaylists('missing-track'), (error) => {
		assert.equal(readApiErrorStatus(error), 404);
		return true;
	});
});
