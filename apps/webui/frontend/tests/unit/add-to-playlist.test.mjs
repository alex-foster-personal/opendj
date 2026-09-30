import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://add-to-playlist.example.test';

let addToPlaylist;
let originalFetch;

function addResponse(stableIds) {
	return {
		playlist_id: 'pl-dest',
		name: 'Warmup',
		vendor: 'local',
		vendor_pl_id: 'v-1',
		created_at: '2026-08-01T00:00:00Z',
		updated_at: '2026-08-01T00:00:01Z',
		forbid_duplicates: false,
		added: stableIds.map((sid, i) => ({ item_id: `item-${i}`, stable_id: sid, order_key: `k${i}` }))
	};
}

function jsonResponse(body, init = {}) {
	return new Response(JSON.stringify(body), {
		status: 200,
		...init,
		headers: { 'content-type': 'application/json', ...(init.headers ?? {}) }
	});
}

function playlistNode(overrides = {}) {
	return {
		playlist_id: 'pl-1',
		name: 'Warmup',
		track_count: 3,
		broken_count: 0,
		kind: 'playlist',
		children: [],
		...overrides
	};
}

before(async () => {
	addToPlaylist = await loadTypeScriptModule('src/lib/rb/add-to-playlist.ts', {
		viteApiBase: API_BASE
	});
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('[if] appendTracksToPlaylist is called with dest id and selected ids [then] it POSTs items:add without GET or If-Match', async () => {
	const calls = [];
	globalThis.fetch = async (request) => {
		calls.push(request);
		return jsonResponse(addResponse(['t-a', 't-b']), {
			headers: { etag: '"rev-2"' }
		});
	};

	await addToPlaylist.appendTracksToPlaylist('pl-dest', ['t-a', 't-b']);

	assert.equal(calls.length, 1);
	assert.equal(calls[0].url, `${API_BASE}/api/v1/playlists/pl-dest/items:add`);
	assert.equal(calls[0].method, 'POST');
	assert.equal(calls[0].headers.get('if-match'), null);
	const postBody = await calls[0].clone().json();
	assert.deepEqual(postBody, { stable_ids: ['t-a', 't-b'] });
	assert.equal(calls.some((r) => r.url.endsWith('/tracks/transfer')), false);
	assert.equal(calls.some((r) => r.url.endsWith('/tracks') && r.method === 'PUT'), false);
});

test('[if] POST returns 409 already_exists [then] it throws and fetch is not called again', async () => {
	let fetchCount = 0;
	globalThis.fetch = async () => {
		fetchCount += 1;
		return jsonResponse(
			{ error: 'already_exists', message: 'track t-a is already a member' },
			{ status: 409, statusText: 'Conflict' }
		);
	};

	await assert.rejects(
		addToPlaylist.appendTracksToPlaylist('pl-dest', ['t-a']),
		/add to playlist pl-dest failed \(409\):/
	);
	assert.equal(fetchCount, 1);
});

test('[if] POST returns 500 [then] it throws and fetch is not called again', async () => {
	let fetchCount = 0;
	globalThis.fetch = async () => {
		fetchCount += 1;
		return jsonResponse({ error: 'failed', message: 'status 500' }, { status: 500 });
	};

	await assert.rejects(
		addToPlaylist.appendTracksToPlaylist('pl-dest', ['t-a']),
		/add to playlist pl-dest failed/
	);
	assert.equal(fetchCount, 1);
});

test('toast helper formats singular and plural counts', () => {
	assert.equal(addToPlaylist.addToPlaylistToastMessage(1, 'Warmup'), 'Added 1 track to "Warmup"');
	assert.equal(addToPlaylist.addToPlaylistToastMessage(3, 'Warmup'), 'Added 3 tracks to "Warmup"');
});

test('writablePlaylistNodes keeps playlist kind only', () => {
	const nodes = [
		playlistNode({ kind: 'playlist' }),
		playlistNode({ kind: 'all_tracks', playlist_id: 'all', name: 'All Tracks' }),
		playlistNode({ kind: 'folder', playlist_id: 'folder-1', name: 'Folder' }),
		playlistNode({ kind: 'missing_tracks', playlist_id: 'missing', name: 'Missing Tracks' })
	];
	const writable = addToPlaylist.writablePlaylistNodes(nodes);
	assert.equal(writable.length, 1);
	assert.equal(writable[0].kind, 'playlist');
});

test('filterPlaylistsByName is case-insensitive substring and empty query returns all', () => {
	const nodes = [
		playlistNode({ name: 'Warmup Set' }),
		playlistNode({ playlist_id: 'pl-2', name: 'Peak Hour' })
	];
	assert.deepEqual(addToPlaylist.filterPlaylistsByName(nodes, 'warm'), [nodes[0]]);
	assert.deepEqual(addToPlaylist.filterPlaylistsByName(nodes, ''), nodes);
});

test('search threshold constant is 15', () => {
	assert.equal(addToPlaylist.PLAYLIST_PICKER_SEARCH_THRESHOLD, 15);
});
