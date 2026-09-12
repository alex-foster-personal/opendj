import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://playlist-add.example.test';

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

test('addPlaylistItems POSTs items:add without if-match', async () => {
	const calls = [];
	globalThis.fetch = async (request) => {
		calls.push(request);
		return jsonResponse(playlistRow({ items: ['t-a', 't-b'] }), {
			headers: { etag: '"rev-2"' }
		});
	};

	const result = await playlistWrite.addPlaylistItems('pl-1', ['t-a', 't-b']);

	assert.equal(calls.length, 1);
	assert.equal(calls[0].url, `${API_BASE}/api/v1/playlists/pl-1/items:add`);
	assert.equal(calls[0].method, 'POST');
	assert.equal(calls[0].headers.get('if-match'), null);
	const body = await calls[0].clone().json();
	assert.deepEqual(body, { stable_ids: ['t-a', 't-b'] });
	assert.equal(result.etag, '"rev-2"');
});

test('addPlaylistItems includes optional position in body', async () => {
	globalThis.fetch = async (request) => {
		const body = await request.clone().json();
		assert.deepEqual(body, { stable_ids: ['t-a'], position: 2 });
		return jsonResponse(playlistRow(), { headers: { etag: '"rev-1"' } });
	};

	await playlistWrite.addPlaylistItems('pl-1', ['t-a'], 2);
});

test('409 already_exists throws add-to-playlist error not PlaylistConflictError', async () => {
	let fetchCount = 0;
	globalThis.fetch = async () => {
		fetchCount += 1;
		return jsonResponse(
			{ error: 'already_exists', message: 'track t-a is already a member' },
			{ status: 409, statusText: 'Conflict' }
		);
	};

	const caught = await playlistWrite.addPlaylistItems('pl-1', ['t-a']).then(
		() => null,
		(error) => error
	);

	assert.ok(!(caught instanceof playlistWrite.PlaylistConflictError));
	assert.match(String(caught), /add to playlist pl-1 failed \(409\):/);
	assert.equal(fetchCount, 1);
});
