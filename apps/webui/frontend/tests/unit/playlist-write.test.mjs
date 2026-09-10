/**
 * Wire-shape regression for the playlist membership write client after its
 * conversion onto the generated OpenAPI client. The daemon contract (URL,
 * If-Match header, request body, ETag reads, the top-level 409 ConflictBody)
 * must be byte-identical to the hand-rolled fetch it replaced.
 *
 * openapi-fetch calls `fetch(request)` with a single Request object, so
 * assertions read `request.url` / `request.method` / `request.headers`.
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://playlist-write.example.test';

let playlistWrite;
let originalFetch;

function playlistRow(overrides = {}) {
	return {
		playlist_id: 'pl-1',
		name: 'Warmup',
		vendor: 'local',
		vendor_pl_id: 'v-1',
		items: ['t-1', 't-2'],
		track_count: 2,
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

test('getPlaylistTracksEtag encodes the id and returns detail plus the ETag header', async () => {
	let seen;
	globalThis.fetch = async (request) => {
		seen = request;
		return jsonResponse({ playlist_id: 'pl/1 a', tracks: [] }, { headers: { etag: '"rev-1"' } });
	};

	const { detail, etag } = await playlistWrite.getPlaylistTracksEtag('pl/1 a');

	assert.equal(seen.url, `${API_BASE}/api/v1/playlists/pl%2F1%20a`);
	assert.equal(seen.method, 'GET');
	assert.equal(etag, '"rev-1"');
	assert.deepEqual(detail.tracks, []);
});

test('getPlaylistTracksEtag refuses a response without an ETag header', async () => {
	globalThis.fetch = async () => jsonResponse({ playlist_id: 'pl-1', tracks: [] });

	await assert.rejects(
		playlistWrite.getPlaylistTracksEtag('pl-1'),
		/playlist pl-1: GET response carries no ETag header/
	);
});

test('replacePlaylistTracks binds the CAS header and the full membership body', async () => {
	let seen;
	let body;
	globalThis.fetch = async (request) => {
		seen = request;
		body = await request.clone().json();
		return jsonResponse(playlistRow({ items: ['t-2', 't-1'] }), { headers: { etag: '"rev-2"' } });
	};

	const result = await playlistWrite.replacePlaylistTracks('pl-1', '"rev-1"', ['t-2', 't-1']);

	assert.equal(seen.url, `${API_BASE}/api/v1/playlists/pl-1/tracks`);
	assert.equal(seen.method, 'PUT');
	assert.equal(seen.headers.get('if-match'), '"rev-1"');
	assert.equal(seen.headers.get('content-type'), 'application/json');
	assert.deepEqual(body, { stable_ids: ['t-2', 't-1'] });
	assert.deepEqual(result, { items: ['t-2', 't-1'], etag: '"rev-2"' });
});

test('a stale If-Match surfaces the top-level ConflictBody as PlaylistConflictError', async () => {
	const current = playlistRow();
	globalThis.fetch = async () =>
		jsonResponse(
			{ error: 'conflict', message: 'playlist If-Match mismatch', current, etag: '"rev-9"' },
			{ status: 409, statusText: 'Conflict' }
		);

	const caught = await playlistWrite.replacePlaylistTracks('pl-1', '"rev-1"', ['t-1']).then(
		() => null,
		(error) => error
	);

	assert.ok(
		caught instanceof playlistWrite.PlaylistConflictError,
		'expected a PlaylistConflictError'
	);
	assert.deepEqual(caught.current, current);
	assert.equal(caught.etag, '"rev-9"');
});

test('a non-409 failure keeps the old message shape with the top-level body message', async () => {
	globalThis.fetch = async () =>
		jsonResponse({ error: 'bad_request', message: 'unknown stable_id t-9' }, { status: 400 });

	await assert.rejects(
		playlistWrite.replacePlaylistTracks('pl-1', '"rev-1"', ['t-9']),
		/update playlist pl-1 failed \(400\): unknown stable_id t-9/
	);
});

test('createPlaylist posts the name and returns the created row', async () => {
	let seen;
	let body;
	globalThis.fetch = async (request) => {
		seen = request;
		body = await request.clone().json();
		return jsonResponse(playlistRow({ name: 'Fresh' }), { status: 201 });
	};

	const row = await playlistWrite.createPlaylist('Fresh');

	assert.equal(seen.url, `${API_BASE}/api/v1/playlists`);
	assert.equal(seen.method, 'POST');
	assert.deepEqual(body, { name: 'Fresh' });
	assert.equal(row.name, 'Fresh');
});

test('renamePlaylist patches with If-Match and the new name', async () => {
	let seen;
	let body;
	globalThis.fetch = async (request) => {
		seen = request;
		body = await request.clone().json();
		return jsonResponse(playlistRow({ name: 'Renamed' }));
	};

	const row = await playlistWrite.renamePlaylist('pl-1', '"rev-1"', 'Renamed');

	assert.equal(seen.url, `${API_BASE}/api/v1/playlists/pl-1`);
	assert.equal(seen.method, 'PATCH');
	assert.equal(seen.headers.get('if-match'), '"rev-1"');
	assert.deepEqual(body, { name: 'Renamed' });
	assert.equal(row.name, 'Renamed');
});

test('transferPlaylistTracks posts to transfer with CAS header and move body', async () => {
	let seen;
	let body;
	globalThis.fetch = async (request) => {
		seen = request;
		body = await request.clone().json();
		return jsonResponse(
			{
				dest: playlistRow({ items: ['t-2', 't-1'] }),
				source: playlistRow({ playlist_id: 'pl-src', items: [] })
			},
			{ headers: { etag: '"rev-transfer"' } }
		);
	};

	const result = await playlistWrite.transferPlaylistTracks('pl-dest', '"rev-dest"', {
		stable_ids: ['t-1'],
		mode: 'move',
		source_playlist_id: 'pl-src',
		source_etag: '"rev-src"'
	});

	assert.equal(seen.url, `${API_BASE}/api/v1/playlists/pl-dest/tracks/transfer`);
	assert.equal(seen.method, 'POST');
	assert.equal(seen.headers.get('if-match'), '"rev-dest"');
	assert.deepEqual(body, {
		stable_ids: ['t-1'],
		mode: 'move',
		source_playlist_id: 'pl-src',
		source_etag: '"rev-src"'
	});
	assert.equal(result.etag, '"rev-transfer"');
	assert.deepEqual(result.dest.items, ['t-2', 't-1']);
});

test('transferPlaylistTracks maps 409 to PlaylistConflictError', async () => {
	const current = playlistRow();
	globalThis.fetch = async () =>
		jsonResponse(
			{ error: 'conflict', message: 'playlist If-Match mismatch', current, etag: '"rev-9"' },
			{ status: 409, statusText: 'Conflict' }
		);

	const caught = await playlistWrite
		.transferPlaylistTracks('pl-1', '"rev-1"', { stable_ids: ['t-1'], mode: 'add' })
		.then(() => null, (error) => error);

	assert.ok(caught instanceof playlistWrite.PlaylistConflictError);
	assert.deepEqual(caught.current, current);
});

test('transferPlaylistTracks refuses a response without an ETag header', async () => {
	globalThis.fetch = async () =>
		jsonResponse({ dest: playlistRow(), source: null });

	await assert.rejects(
		playlistWrite.transferPlaylistTracks('pl-1', '"rev-1"', {
			stable_ids: ['t-1'],
			mode: 'add'
		}),
		/playlist pl-1: POST transfer response carries no ETag header/
	);
});

test('deletePlaylist sends If-Match and accepts the bodyless 204', async () => {
	let seen;
	globalThis.fetch = async (request) => {
		seen = request;
		return new Response(null, { status: 204 });
	};

	await playlistWrite.deletePlaylist('pl-1', '"rev-1"');

	assert.equal(seen.url, `${API_BASE}/api/v1/playlists/pl-1`);
	assert.equal(seen.method, 'DELETE');
	assert.equal(seen.headers.get('if-match'), '"rev-1"');
});
