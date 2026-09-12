import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://add-to-playlist.example.test';

let addToPlaylist;
let playlistWrite;
let originalFetch;

function playlistRow(overrides = {}) {
	return {
		playlist_id: 'pl-dest',
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
	playlistWrite = await loadTypeScriptModule('src/lib/rb/playlist-write.ts', {
		viteApiBase: API_BASE
	});
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('[if] appendTracksToPlaylist is called with dest id and selected ids [then] it GETs and POSTs transfer add without PUT', async () => {
	const calls = [];
	globalThis.fetch = async (request) => {
		calls.push(request);
		if (request.method === 'GET') {
			return jsonResponse({ playlist_id: 'pl-dest', tracks: [] }, { headers: { etag: '"rev-1"' } });
		}
		const body = await request.clone().json();
		return jsonResponse(
			{ dest: playlistRow({ items: ['t-existing', 't-a', 't-b'] }), source: null },
			{ headers: { etag: '"rev-2"' } }
		);
	};

	await addToPlaylist.appendTracksToPlaylist('pl-dest', ['t-a', 't-b']);

	assert.equal(calls.length, 2);
	assert.equal(calls[0].url, `${API_BASE}/api/v1/playlists/pl-dest`);
	assert.equal(calls[0].method, 'GET');
	assert.equal(calls[1].url, `${API_BASE}/api/v1/playlists/pl-dest/tracks/transfer`);
	assert.equal(calls[1].method, 'POST');
	assert.equal(calls[1].headers.get('if-match'), '"rev-1"');
	const postBody = await calls[1].clone().json();
	assert.deepEqual(postBody, { stable_ids: ['t-a', 't-b'], mode: 'add' });
	assert.equal(calls.some((r) => r.url.endsWith('/tracks') && r.method === 'PUT'), false);
});

test('[if] POST returns 409 [then] it throws and fetch is not called again', async () => {
	let fetchCount = 0;
	globalThis.fetch = async (request) => {
		fetchCount += 1;
		if (request.method === 'GET') {
			return jsonResponse({ playlist_id: 'pl-dest', tracks: [] }, { headers: { etag: '"rev-1"' } });
		}
		return jsonResponse(
			{
				error: 'conflict',
				message: 'playlist If-Match mismatch',
				current: playlistRow(),
				etag: '"rev-9"'
			},
			{ status: 409, statusText: 'Conflict' }
		);
	};

	const caught = await addToPlaylist.appendTracksToPlaylist('pl-dest', ['t-a']).then(
		() => null,
		(error) => error
	);

	assert.ok(
		caught instanceof playlistWrite.PlaylistConflictError ||
			caught?.name === 'PlaylistConflictError' ||
			String(caught).includes('If-Match mismatch')
	);
	assert.equal(fetchCount, 2);
});

test('[if] POST returns 428 or 500 [then] it throws and fetch is not called again', async () => {
	for (const status of [428, 500]) {
		let fetchCount = 0;
		globalThis.fetch = async (request) => {
			fetchCount += 1;
			if (request.method === 'GET') {
				return jsonResponse({ playlist_id: 'pl-dest', tracks: [] }, { headers: { etag: '"rev-1"' } });
			}
			return jsonResponse({ error: 'failed', message: `status ${status}` }, { status });
		};

		await assert.rejects(
			addToPlaylist.appendTracksToPlaylist('pl-dest', ['t-a']),
			/transfer tracks to playlist pl-dest failed/
		);
		assert.equal(fetchCount, 2);
	}
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
	assert.deepEqual(
		addToPlaylist.filterPlaylistsByName(nodes, 'warm'),
		[nodes[0]]
	);
	assert.deepEqual(addToPlaylist.filterPlaylistsByName(nodes, ''), nodes);
});

test('search threshold constant is 15', () => {
	assert.equal(addToPlaylist.PLAYLIST_PICKER_SEARCH_THRESHOLD, 15);
});
