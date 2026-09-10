/**
 * Wire-shape regression for the playlist history client. URLs must stay on
 * /api/v1/playlist-history (never /playlists/history). 409 nothing_to_undo /
 * nothing_to_redo map to PlaylistHistoryEmptyError; snapshot mismatch reuses
 * PlaylistConflictError; a missing body is loud.
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://playlist-history.example.test';

let playlistHistory;
let originalFetch;

function jsonResponse(body, init = {}) {
	return new Response(JSON.stringify(body), {
		status: 200,
		...init,
		headers: { 'content-type': 'application/json', ...(init.headers ?? {}) }
	});
}

function historyBody(overrides = {}) {
	return {
		cursor: 1,
		limit: 50,
		can_undo: true,
		can_redo: false,
		entries: [
			{
				command_id: 'aa',
				op: 'create',
				playlist_id: 'pl-1',
				ts: '2026-01-01T00:00:00+00:00',
				label: "Create 'Warmup'"
			}
		],
		...overrides
	};
}

function applyBody(overrides = {}) {
	return {
		command_id: 'aa',
		op: 'rename',
		action: 'undo',
		playlist_id: 'pl-1',
		current: {
			playlist_id: 'pl-1',
			name: 'Warmup',
			vendor: 'webui',
			vendor_pl_id: 'v1',
			items: [],
			track_count: 0,
			created_at: '2026-01-01T00:00:00Z',
			updated_at: '2026-01-01T00:00:01Z'
		},
		etag: '"rev-2"',
		can_undo: true,
		can_redo: true,
		...overrides
	};
}

before(async () => {
	playlistHistory = await loadTypeScriptModule('src/lib/rb/playlist-history.ts', {
		viteApiBase: API_BASE
	});
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('fetchPlaylistHistory hits GET /api/v1/playlist-history', async () => {
	let seen;
	globalThis.fetch = async (request) => {
		seen = request;
		return jsonResponse(historyBody());
	};

	const hist = await playlistHistory.fetchPlaylistHistory();

	assert.equal(seen.url, `${API_BASE}/api/v1/playlist-history`);
	assert.equal(seen.method, 'GET');
	assert.equal(hist.can_undo, true);
	assert.equal(hist.entries[0].label, "Create 'Warmup'");
});

test('undoPlaylistEdit posts /playlist-history/undo', async () => {
	let seen;
	globalThis.fetch = async (request) => {
		seen = request;
		return jsonResponse(applyBody());
	};

	const result = await playlistHistory.undoPlaylistEdit();

	assert.equal(seen.url, `${API_BASE}/api/v1/playlist-history/undo`);
	assert.equal(seen.method, 'POST');
	assert.equal(result.action, 'undo');
	assert.equal(result.current.name, 'Warmup');
});

test('redoPlaylistEdit posts /playlist-history/redo', async () => {
	let seen;
	globalThis.fetch = async (request) => {
		seen = request;
		return jsonResponse(applyBody({ action: 'redo', op: 'memberships' }));
	};

	const result = await playlistHistory.redoPlaylistEdit();

	assert.equal(seen.url, `${API_BASE}/api/v1/playlist-history/redo`);
	assert.equal(seen.method, 'POST');
	assert.equal(result.action, 'redo');
});

test('409 nothing_to_undo maps to PlaylistHistoryEmptyError', async () => {
	globalThis.fetch = async () =>
		jsonResponse({ error: 'nothing_to_undo' }, { status: 409, statusText: 'Conflict' });

	const caught = await playlistHistory.undoPlaylistEdit().then(
		() => null,
		(error) => error
	);

	assert.ok(
		caught instanceof playlistHistory.PlaylistHistoryEmptyError,
		'expected PlaylistHistoryEmptyError'
	);
	assert.equal(caught.code, 'nothing_to_undo');
});

test('409 nothing_to_redo maps to PlaylistHistoryEmptyError', async () => {
	globalThis.fetch = async () =>
		jsonResponse({ error: 'nothing_to_redo' }, { status: 409, statusText: 'Conflict' });

	const caught = await playlistHistory.redoPlaylistEdit().then(
		() => null,
		(error) => error
	);

	assert.ok(caught instanceof playlistHistory.PlaylistHistoryEmptyError);
	assert.equal(caught.code, 'nothing_to_redo');
});

test('409 conflict reuses PlaylistConflictError', async () => {
	const current = applyBody().current;
	globalThis.fetch = async () =>
		jsonResponse(
			{ error: 'conflict', message: 'snapshot mismatch', current, etag: '"rev-9"' },
			{ status: 409, statusText: 'Conflict' }
		);

	const caught = await playlistHistory.undoPlaylistEdit().then(
		() => null,
		(error) => error
	);

	assert.ok(caught instanceof playlistHistory.PlaylistConflictError);
	assert.equal(caught.etag, '"rev-9"');
	assert.deepEqual(caught.current, current);
});

test('a missing undo body is loud', async () => {
	globalThis.fetch = async () =>
		new Response(null, { status: 200, headers: { 'content-length': '0' } });

	await assert.rejects(playlistHistory.undoPlaylistEdit(), /no body|EMPTY_BODY|answered 200/);
});
