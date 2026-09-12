import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://playlist-remove.example.test';

let playlistWrite;
let originalFetch;

function playlistRow(overrides = {}) {
	return {
		playlist_id: 'pl-1',
		name: 'Warmup',
		vendor: 'local',
		vendor_pl_id: 'v-1',
		items: ['t-existing'],
		track_count: 1,
		created_at: '2026-08-01T00:00:00Z',
		updated_at: '2026-08-01T00:00:01Z',
		...overrides
	};
}

function jsonResponse(body, init = {}) {
	return new Response(JSON.stringify(body), {
		status: 200,
		...init,
		headers: { 'content-type': 'application/json', ...(init.headers ?? {}) }
	});
}

before(async () => {
	playlistWrite = await loadTypeScriptModule('src/lib/rb/playlist-write.ts', {
		viteApiBase: API_BASE
	});
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('deletePlaylistItem DELETEs by item_id without if-match', async () => {
	const calls = [];
	globalThis.fetch = async (request) => {
		calls.push(request);
		return jsonResponse(playlistRow({ items: [] }), {
			headers: { etag: '"rev-2"' }
		});
	};

	const result = await playlistWrite.deletePlaylistItem('pl-1', 'item-1');

	assert.equal(calls.length, 1);
	assert.equal(calls[0].url, `${API_BASE}/api/v1/playlists/pl-1/items/item-1`);
	assert.equal(calls[0].method, 'DELETE');
	assert.equal(calls[0].headers.get('if-match'), null);
	assert.equal(result.etag, '"rev-2"');
});

test('404 throws remove-from-playlist error not PlaylistConflictError', async () => {
	globalThis.fetch = async () =>
		jsonResponse(
			{ error: 'not_found', message: 'playlist pl-1 has no membership item-1' },
			{ status: 404, statusText: 'Not Found' }
		);

	const caught = await playlistWrite.deletePlaylistItem('pl-1', 'item-1').then(
		() => null,
		(error) => error
	);
	assert.ok(caught instanceof Error);
	assert.match(String(caught), /remove from playlist pl-1 failed \(404\):/);
	assert.notEqual(caught.name, 'PlaylistConflictError');
});

test('422 smartlist_immutable throws remove error not PlaylistConflictError', async () => {
	globalThis.fetch = async () =>
		jsonResponse(
			{ error: 'smartlist_immutable', message: 'cannot remove tracks from a smartlist' },
			{ status: 422, statusText: 'Unprocessable Entity' }
		);

	const caught = await playlistWrite.deletePlaylistItem('pl-1', 'item-1').then(
		() => null,
		(error) => error
	);
	assert.ok(caught instanceof Error);
	assert.match(String(caught), /remove from playlist pl-1 failed \(422\):/);
	assert.notEqual(caught.name, 'PlaylistConflictError');
});
