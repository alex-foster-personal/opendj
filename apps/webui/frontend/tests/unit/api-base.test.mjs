/**
 * Wire-shape regression for src/lib/api.ts after its conversion onto the
 * generated OpenAPI client -- the last module in the fleet to convert, and the
 * one every other frontend module imports API_BASE from.
 *
 * Two things are pinned here. First the daemon contract (URL, query, method,
 * If-Match header, request body, and the exact error each failure maps onto),
 * which must be byte-identical to the hand-rolled fetch it replaced.
 * tests/unit/smartlists-api.test.mjs covers the smartlist half of the module.
 *
 * Second, that api.ts no longer resolves its own base URL: its API_BASE is now
 * a re-export of the client's, so the two can never drift apart again.
 *
 * openapi-fetch calls `fetch(request)` with a single Request object, so
 * assertions read `request.url` / `request.method` / `request.headers` instead
 * of the `(input, init)` pair the pre-conversion tests used.
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://api-base.example.test';

let api;
let originalFetch;

function jsonResponse(body, init = {}) {
	return new Response(JSON.stringify(body), {
		status: 200,
		...init,
		headers: { 'content-type': 'application/json', ...(init.headers ?? {}) }
	});
}

/** Answer every call with `body`, recording the Request the client sent. */
function captureRequest(body, init = {}) {
	const captured = {};
	globalThis.fetch = async (request) => {
		captured.request = request;
		captured.body = request.body === null ? null : await request.clone().json();
		return jsonResponse(body, init);
	};
	return captured;
}

before(async () => {
	api = await loadTypeScriptModule('src/lib/api.ts', { viteApiBase: API_BASE });
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

// --------------------------------------------------------------------------
// API_BASE is one value, resolved once
// --------------------------------------------------------------------------

test('API_BASE is the client\'s own value, not a second resolution of it', async () => {
	const client = await loadTypeScriptModule('src/lib/api/client.ts', { viteApiBase: API_BASE });

	assert.ok(
		Object.is(api.API_BASE, client.API_BASE),
		`api.ts API_BASE ${JSON.stringify(api.API_BASE)} is not the client's ${JSON.stringify(client.API_BASE)}`
	);
	assert.equal(api.API_BASE, API_BASE);
});

test('API_BASE falls back to same-origin identically on both sides', async () => {
	const sameOriginApi = await loadTypeScriptModule('src/lib/api.ts');
	const sameOriginClient = await loadTypeScriptModule('src/lib/api/client.ts');

	assert.ok(Object.is(sameOriginApi.API_BASE, sameOriginClient.API_BASE));
	assert.equal(sameOriginApi.API_BASE, '');
});

// --------------------------------------------------------------------------
// URL construction and method
// --------------------------------------------------------------------------

test('listTracks drops empty, null and undefined filters from the query', async () => {
	const seen = captureRequest({ items: [], next_cursor: null });

	await api.listTracks({ q: 'aphex', bpm_min: 120, key: '', tag: null, cursor: undefined });

	assert.equal(seen.request.url, `${API_BASE}/api/v1/tracks?q=aphex&bpm_min=120`);
	assert.equal(seen.request.method, 'GET');
});

test('listTracks emits no question mark when every filter is dropped', async () => {
	const seen = captureRequest({ items: [], next_cursor: null });

	await api.listTracks({ q: '' });

	assert.equal(seen.request.url, `${API_BASE}/api/v1/tracks`);
});

test('getTrack encodes the stable id in the path and returns the ETag', async () => {
	const seen = captureRequest({ stable_id: 'sid/1' }, { headers: { etag: '"rev-1"' } });

	const got = await api.getTrack('sid/1 a');

	assert.equal(seen.request.url, `${API_BASE}/api/v1/tracks/sid%2F1%20a`);
	assert.equal(got.etag, '"rev-1"');
	assert.equal(got.track.stable_id, 'sid/1');
});

test('listPairings omits the query entirely when no source is given', async () => {
	const seen = captureRequest([]);

	await api.listPairings();

	assert.equal(seen.request.url, `${API_BASE}/api/v1/pairings`);
});

test('listPairings encodes a source filter', async () => {
	const seen = captureRequest([]);

	await api.listPairings('a i');

	assert.equal(seen.request.url, `${API_BASE}/api/v1/pairings?source=a%20i`);
});

test('getQueue and getSettings hit their documented paths', async () => {
	const queue = captureRequest({ items: [], note: null });
	await api.getQueue('next up');
	assert.equal(queue.request.url, `${API_BASE}/api/v1/queues/next%20up`);

	const settings = captureRequest({ groups: [] });
	await api.getSettings();
	assert.equal(settings.request.url, `${API_BASE}/api/v1/settings`);
});

test('getHealth surfaces the x-bind-warning response header', async () => {
	captureRequest({ status: 'ok', version: '1' }, { headers: { 'x-bind-warning': 'bound to 0.0.0.0' } });

	const { health, bindWarning } = await api.getHealth();

	assert.equal(health.status, 'ok');
	assert.equal(bindWarning, 'bound to 0.0.0.0');
});

// --------------------------------------------------------------------------
// Method, headers and request body
// --------------------------------------------------------------------------

test('patchTrack sends PATCH with the If-Match header and a JSON body', async () => {
	const seen = captureRequest({ stable_id: 'sid-1' }, { headers: { etag: '"rev-2"' } });

	const saved = await api.patchTrack('sid-1', '"rev-1"', { rating: 4, tags_add: ['peak'] });

	assert.equal(seen.request.url, `${API_BASE}/api/v1/tracks/sid-1`);
	assert.equal(seen.request.method, 'PATCH');
	assert.equal(seen.request.headers.get('if-match'), '"rev-1"');
	assert.equal(seen.request.headers.get('content-type'), 'application/json');
	assert.deepEqual(seen.body, { rating: 4, tags_add: ['peak'] });
	assert.equal(saved.etag, '"rev-2"');
});

test('solvePlayIt posts the goal to the play-it route', async () => {
	const seen = captureRequest({ playlist_id: 'pl-1', etag: '"rev-1"', steps: [] });

	await api.solvePlayIt('pl/1', { duration_min: 90, peak_at_min: 60 });

	assert.equal(seen.request.url, `${API_BASE}/api/v1/play-it/pl%2F1/solve`);
	assert.equal(seen.request.method, 'POST');
	assert.deepEqual(seen.body, { duration_min: 90, peak_at_min: 60 });
});

test('replacePlaylistTracks PUTs the complete membership under If-Match', async () => {
	const seen = captureRequest({ playlist_id: 'pl-1', items: ['a', 'b'] }, {
		headers: { etag: '"rev-2"' }
	});

	const applied = await api.replacePlaylistTracks('pl-1', ['a', 'b'], '"rev-1"');

	assert.equal(seen.request.url, `${API_BASE}/api/v1/playlists/pl-1/tracks`);
	assert.equal(seen.request.method, 'PUT');
	assert.equal(seen.request.headers.get('if-match'), '"rev-1"');
	assert.deepEqual(seen.body, { stable_ids: ['a', 'b'] });
	assert.equal(applied.etag, '"rev-2"');
});

test('createPairing posts the body and deletePairing sends DELETE with If-Match', async () => {
	const created = captureRequest({ pairing_id: 'pair-1' }, { status: 201 });
	await api.createPairing({ from_stable_id: 'a', to_stable_id: 'b' });
	assert.equal(created.request.method, 'POST');
	assert.deepEqual(created.body, { from_stable_id: 'a', to_stable_id: 'b' });

	let seen;
	globalThis.fetch = async (request) => {
		seen = request;
		return new Response(null, { status: 204 });
	};
	await api.deletePairing('pair/1', '"rev-1"');

	assert.equal(seen.url, `${API_BASE}/api/v1/pairings/pair%2F1`);
	assert.equal(seen.method, 'DELETE');
	assert.equal(seen.headers.get('if-match'), '"rev-1"');
});

// --------------------------------------------------------------------------
// Error mapping: every exported error class still fires on the same condition
// --------------------------------------------------------------------------

test('a stale patchTrack surfaces ConflictError carrying the server copy', async () => {
	globalThis.fetch = async () =>
		jsonResponse(
			{ current: { stable_id: 'sid-1', rating: 5 }, etag: '"rev-9"' },
			{ status: 409, statusText: 'Conflict' }
		);

	const caught = await api.patchTrack('sid-1', '"rev-1"', { rating: 4 }).then(
		() => null,
		(error) => error
	);

	assert.ok(caught instanceof api.ConflictError, 'expected a ConflictError');
	assert.equal(caught.etag, '"rev-9"');
	assert.equal(caught.current.rating, 5);
});

test('any other patchTrack failure keeps the bare-status message', async () => {
	globalThis.fetch = async () =>
		jsonResponse({ detail: 'nope' }, { status: 500, statusText: 'Internal Server Error' });

	await assert.rejects(
		() => api.patchTrack('sid-1', '"rev-1"', { rating: 4 }),
		/^Error: PATCH failed: 500$/
	);
});

test('a failed solve surfaces the route error code, message and details', async () => {
	globalThis.fetch = async () =>
		jsonResponse(
			{ error: 'insufficient_analysis', message: 'only 3 of 40 tracks have energy', details: { analysed: 3 } },
			{ status: 422, statusText: 'Unprocessable Entity' }
		);

	const caught = await api.solvePlayIt('pl-1', { duration_min: 60 }).then(
		() => null,
		(error) => error
	);

	assert.ok(caught instanceof api.PlayItError, 'expected a PlayItError');
	assert.equal(caught.code, 'insufficient_analysis');
	assert.equal(caught.message, 'only 3 of 40 tracks have energy');
	assert.deepEqual(caught.details, { analysed: 3 });
});

test('a solve failure with no envelope still fails loudly rather than silently', async () => {
	globalThis.fetch = async () => new Response(null, { status: 502, statusText: 'Bad Gateway' });

	const caught = await api.solvePlayIt('pl-1', { duration_min: 60 }).then(
		() => null,
		(error) => error
	);

	assert.ok(caught instanceof api.PlayItError, 'expected a PlayItError');
	assert.equal(caught.code, 'unknown');
	assert.equal(caught.message, 'solve failed: 502');
});

test('a stale reorder surfaces PlaylistConflictError so the caller re-solves', async () => {
	globalThis.fetch = async () =>
		jsonResponse(
			{ current: { playlist_id: 'pl-1', items: ['b', 'a'] }, etag: '"rev-9"' },
			{ status: 409, statusText: 'Conflict' }
		);

	const caught = await api.replacePlaylistTracks('pl-1', ['a', 'b'], '"rev-1"').then(
		() => null,
		(error) => error
	);

	assert.ok(caught instanceof api.PlaylistConflictError, 'expected a PlaylistConflictError');
	assert.equal(caught.etag, '"rev-9"');
	assert.deepEqual(caught.current.items, ['b', 'a']);
});

test('any other reorder failure keeps the bare-status message', async () => {
	globalThis.fetch = async () => jsonResponse({ detail: 'nope' }, { status: 422 });

	await assert.rejects(
		() => api.replacePlaylistTracks('pl-1', ['a'], '"rev-1"'),
		/^Error: apply reorder failed: 422$/
	);
});

test('createPairing and deletePairing keep their own failure messages', async () => {
	globalThis.fetch = async () => jsonResponse({ detail: 'nope' }, { status: 422 });
	await assert.rejects(
		() => api.createPairing({ from_stable_id: 'a', to_stable_id: 'b' }),
		/^Error: create pairing failed: 422$/
	);

	globalThis.fetch = async () => jsonResponse({ detail: 'gone' }, { status: 404 });
	await assert.rejects(() => api.deletePairing('pair-1', '"rev-1"'), /^Error: delete failed: 404$/);
});

test('a 2xx that is not 204 still fails a delete, as before the conversion', async () => {
	globalThis.fetch = async () => jsonResponse({ ok: true }, { status: 200 });

	await assert.rejects(() => api.deletePairing('pair-1', '"rev-1"'), /^Error: delete failed: 200$/);
});

test('a transport fault propagates unwrapped, never masked as an HTTP answer', async () => {
	globalThis.fetch = async () => {
		throw new TypeError('fetch failed');
	};

	await assert.rejects(() => api.listTracks(), /fetch failed/);
	await assert.rejects(() => api.createPairing({ from_stable_id: 'a', to_stable_id: 'b' }), /fetch failed/);
});
