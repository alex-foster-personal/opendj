/**
 * Wire-shape regression for the playlist undelete client (LIBMX-03).
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://playlist-deleted.example.test';

let playlistDeleted;
let originalFetch;

function jsonResponse(body, init = {}) {
	return new Response(JSON.stringify(body), {
		status: 200,
		...init,
		headers: { 'content-type': 'application/json', ...(init.headers ?? {}) }
	});
}

before(async () => {
	playlistDeleted = await loadTypeScriptModule('src/lib/rb/playlist-deleted.ts', {
		viteApiBase: API_BASE
	});
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('listDeletedPlaylists GETs /api/v1/playlists/deleted', async () => {
	let seen;
	globalThis.fetch = async (request) => {
		seen = request;
		return jsonResponse([
			{
				playlist_id: 'pl-1',
				name: 'Gone',
				vendor: 'webui',
				vendor_pl_id: 'gone',
				deleted_at: '2026-09-13T00:00:00Z',
				updated_at: '2026-09-13T00:00:01Z',
				track_count: 2
			}
		]);
	};

	const rows = await playlistDeleted.listDeletedPlaylists();

	assert.equal(seen.url, `${API_BASE}/api/v1/playlists/deleted`);
	assert.equal(seen.method, 'GET');
	assert.equal(rows.length, 1);
	assert.equal(rows[0].playlist_id, 'pl-1');
});

test('undeletePlaylist POSTs without If-Match', async () => {
	let seen;
	globalThis.fetch = async (request) => {
		seen = request;
		return jsonResponse({
			playlist_id: 'pl-1',
			name: 'Back',
			vendor: 'webui',
			vendor_pl_id: 'back',
			items: ['t-1'],
			track_count: 1,
			created_at: '2026-09-01T00:00:00Z',
			updated_at: '2026-09-13T00:00:02Z',
			forbid_duplicates: false
		});
	};

	const row = await playlistDeleted.undeletePlaylist('pl-1');

	assert.equal(seen.url, `${API_BASE}/api/v1/playlists/pl-1:undelete`);
	assert.equal(seen.method, 'POST');
	assert.equal(seen.headers.get('if-match'), null);
	assert.equal(row.name, 'Back');
});

test('409 and 404 responses throw', async () => {
	globalThis.fetch = async () =>
		new Response(JSON.stringify({ detail: { error: 'not_deleted' } }), {
			status: 409,
			headers: { 'content-type': 'application/json' }
		});
	await assert.rejects(playlistDeleted.undeletePlaylist('pl-live'), /409/);

	globalThis.fetch = async () =>
		new Response(JSON.stringify({ detail: { error: 'not_found' } }), {
			status: 404,
			headers: { 'content-type': 'application/json' }
		});
	await assert.rejects(playlistDeleted.undeletePlaylist('pl-missing'), /404/);
});
