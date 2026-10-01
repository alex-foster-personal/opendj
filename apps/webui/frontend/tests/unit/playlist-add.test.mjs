import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://playlist-add.example.test';

let playlistWrite;
let originalFetch;

function member(stableId, index = 0) {
	return { item_id: `item-${stableId}-${index}`, stable_id: stableId, order_key: `k${index}` };
}

function addResponse(overrides = {}) {
	return {
		playlist_id: 'pl-1',
		name: 'Warmup',
		vendor: 'local',
		vendor_pl_id: 'v-1',
		created_at: '2026-08-01T00:00:00Z',
		updated_at: '2026-08-01T00:00:01Z',
		forbid_duplicates: false,
		added: [member('t-a')],
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
		return jsonResponse(addResponse({ added: [member('t-a', 0), member('t-b', 1)] }), {
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
	assert.deepEqual(
		result.added.map((row) => row.stable_id),
		['t-a', 't-b']
	);
});

test('addPlaylistItems includes optional position in body', async () => {
	globalThis.fetch = async (request) => {
		const body = await request.clone().json();
		assert.deepEqual(body, { stable_ids: ['t-a'], position: 2 });
		return jsonResponse(addResponse(), { headers: { etag: '"rev-1"' } });
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

test('addPlaylistItems accepts 200 adding a second copy of a present track', async () => {
	globalThis.fetch = async () => {
		return jsonResponse(addResponse({ added: [member('t-a', 1)] }), {
			headers: { etag: '"rev-dup"' }
		});
	};

	const result = await playlistWrite.addPlaylistItems('pl-1', ['t-a']);

	assert.deepEqual(result.added, [member('t-a', 1)]);
	assert.equal(result.etag, '"rev-dup"');
});

test('[if] items:add answers without an added array [then] addPlaylistItems throws', async () => {
	globalThis.fetch = async () => {
		return jsonResponse({ playlist_id: 'pl-1', items: ['t-a'] }, { headers: { etag: '"rev-3"' } });
	};

	await assert.rejects(playlistWrite.addPlaylistItems('pl-1', ['t-a']), /carries no added rows/);
});
