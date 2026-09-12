/**
 * LIBM-22: movePlaylistItems posts one :move call with If-Match.
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://playlist-move.example.test';

let playlistWrite;
let originalFetch;
let fetchCount = 0;

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

test('movePlaylistItems POSTs items:move with If-Match and slice body', async () => {
	let seen;
	fetchCount = 0;
	globalThis.fetch = async (request) => {
		fetchCount += 1;
		seen = request;
		return jsonResponse(
			{
				playlist_id: 'pl-1',
				name: 'Warmup',
				vendor: 'local',
				vendor_pl_id: 'v-1',
				items: ['t-2', 't-1'],
				track_count: 2,
				created_at: '2026-08-01T00:00:00Z',
				updated_at: '2026-08-01T00:00:01Z',
				renumbered: false
			},
			{ headers: { etag: '"rev-2"' } }
		);
	};

	const result = await playlistWrite.movePlaylistItems('pl-1', '"rev-1"', {
		range_start: 'item-a',
		range_length: 1,
		range_end: 'item-a',
		after_item_id: 'item-b'
	});

	assert.equal(fetchCount, 1);
	assert.equal(seen.url, `${API_BASE}/api/v1/playlists/pl-1/items:move`);
	assert.equal(seen.method, 'POST');
	assert.equal(seen.headers.get('If-Match'), '"rev-1"');
	const body = JSON.parse(await seen.text());
	assert.deepEqual(body, {
		range_start: 'item-a',
		range_length: 1,
		range_end: 'item-a',
		after_item_id: 'item-b'
	});
	assert.equal(result.etag, '"rev-2"');
});

test('movePlaylistItems maps 409 conflict to PlaylistConflictError', async () => {
	globalThis.fetch = async () =>
		new Response(
			JSON.stringify({
				error: 'conflict',
				message: 'stale',
				current: {
					playlist_id: 'pl-1',
					name: 'Warmup',
					vendor: 'local',
					vendor_pl_id: 'v-1',
					items: [],
					created_at: '2026-08-01T00:00:00Z',
					updated_at: '2026-08-01T00:00:01Z'
				},
				etag: '"fresh"'
			}),
			{ status: 409, headers: { 'content-type': 'application/json' } }
		);

	await assert.rejects(
		playlistWrite.movePlaylistItems('pl-1', '"rev-1"', {
			range_start: 'a',
			range_length: 1,
			range_end: 'a',
			after_item_id: 'b'
		}),
		playlistWrite.PlaylistConflictError
	);
});

test('movePlaylistItems surfaces slice_not_contiguous without PlaylistConflictError', async () => {
	globalThis.fetch = async () =>
		new Response(
			JSON.stringify({
				error: 'slice_not_contiguous',
				message: 'slice not contiguous: gap at item-b'
			}),
			{ status: 422, headers: { 'content-type': 'application/json' } }
		);

	await assert.rejects(
		playlistWrite.movePlaylistItems('pl-1', '"rev-1"', {
			range_start: 'a',
			range_length: 2,
			range_end: 'c',
			after_item_id: 'd'
		}),
		/move playlist pl-1 failed \(422\):/
	);
});
