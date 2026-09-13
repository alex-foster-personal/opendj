import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://playlist-forbid.example.test';

let playlistWrite;
let originalFetch;

function playlistRow(overrides = {}) {
	return {
		playlist_id: 'pl-1',
		name: 'Set',
		vendor: 'webui',
		vendor_pl_id: 'v1',
		items: [],
		track_count: 0,
		created_at: '2026-01-01T00:00:00Z',
		updated_at: '2026-01-01T00:00:00Z',
		forbid_duplicates: false,
		...overrides
	};
}

function jsonResponse(body, init = {}) {
	return new Response(JSON.stringify(body), {
		status: init.status ?? 200,
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

test('patchPlaylist PATCHes forbid_duplicates with If-Match and no name', async () => {
	let seen;
	let body;
	globalThis.fetch = async (request) => {
		seen = request;
		body = await request.clone().json();
		return jsonResponse(playlistRow({ forbid_duplicates: true }));
	};

	const row = await playlistWrite.patchPlaylist('pl-1', '"rev-1"', {
		forbid_duplicates: true
	});

	assert.equal(seen.url, `${API_BASE}/api/v1/playlists/pl-1`);
	assert.equal(seen.method, 'PATCH');
	assert.equal(seen.headers.get('if-match'), '"rev-1"');
	assert.deepEqual(body, { forbid_duplicates: true });
	assert.equal(row.forbid_duplicates, true);
});

test('renamePlaylist still PATCHes name only', async () => {
	let body;
	globalThis.fetch = async (request) => {
		body = await request.clone().json();
		return jsonResponse(playlistRow({ name: 'Renamed' }));
	};

	await playlistWrite.renamePlaylist('pl-1', '"rev-1"', 'Renamed');
	assert.deepEqual(body, { name: 'Renamed' });
});

test('TreeContextMenu wires forbid-duplicates checkbox', () => {
	const src = readFileSync(
		new URL('../../src/lib/components/rb/browser/TreeContextMenu.svelte', import.meta.url),
		'utf8'
	);
	assert.match(src, /id: 'forbid-duplicates'/);
	assert.match(src, /Forbid duplicates/);
});

test('ContextMenu supports menuitemcheckbox via checked', () => {
	const src = readFileSync(
		new URL('../../src/lib/components/rb/ContextMenu.svelte', import.meta.url),
		'utf8'
	);
	assert.match(src, /menuitemcheckbox/);
	assert.match(src, /aria-checked/);
});

test('BrowserPanel defines toggleForbidDuplicates', () => {
	const src = readFileSync(
		new URL('../../src/lib/components/rb/BrowserPanel.svelte', import.meta.url),
		'utf8'
	);
	assert.match(src, /toggleForbidDuplicates/);
	assert.match(src, /forbid_duplicates/);
});
