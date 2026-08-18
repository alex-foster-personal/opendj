/**
 * Wire-shape regression for the reconcile module after its conversion onto the
 * generated OpenAPI client. The daemon contract (URL, query, If-Match header,
 * request body) must be byte-identical to the hand-rolled fetch it replaced.
 *
 * Note for converters: openapi-fetch calls `fetch(request)` with a single
 * Request object, so assertions read `request.url` / `request.method` /
 * `request.headers` instead of the `(input, init)` pair.
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://reconcile-api.example.test';

let reconcileApi;
let originalFetch;

function jsonResponse(body, init = {}) {
	return new Response(JSON.stringify(body), {
		status: 200,
		...init,
		headers: { 'content-type': 'application/json', ...(init.headers ?? {}) }
	});
}

before(async () => {
	reconcileApi = await loadTypeScriptModule('src/lib/reconcile-api.ts', { viteApiBase: API_BASE });
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('listBroken hits the library-wide listing with no query string', async () => {
	let seen;
	globalThis.fetch = async (request) => {
		seen = request;
		return jsonResponse({ total: 0, tracks: [] });
	};

	const page = await reconcileApi.listBroken();

	assert.equal(seen.url, `${API_BASE}/api/v1/reconcile/broken`);
	assert.equal(seen.method, 'GET');
	assert.equal(page.total, 0);
});

test('listBroken scopes to one playlist and encodes the id', async () => {
	let seen;
	globalThis.fetch = async (request) => {
		seen = request;
		return jsonResponse({ total: 0, tracks: [] });
	};

	await reconcileApi.listBroken('pl/1 a');

	assert.equal(seen.url, `${API_BASE}/api/v1/reconcile/broken?playlist_id=pl%2F1%20a`);
});

test('getRelocateCandidates encodes the stable id and sends the limit', async () => {
	let seen;
	globalThis.fetch = async (request) => {
		seen = request;
		return jsonResponse({
			stable_id: 'sid/1',
			original_path: '/old/a.flac',
			vendor_id: '42',
			total: 0,
			candidates: []
		});
	};

	const found = await reconcileApi.getRelocateCandidates('sid/1');

	assert.equal(seen.url, `${API_BASE}/api/v1/relocate/candidates/sid%2F1?limit=5`);
	assert.equal(found.original_path, '/old/a.flac');
});

test('applyRelocate binds the CAS header and the full confirmation body', async () => {
	let seen;
	let body;
	globalThis.fetch = async (request) => {
		seen = request;
		body = await request.clone().json();
		return jsonResponse({
			stable_id: 'sid-1',
			new_path: '/new/a.flac',
			target: 'rekordbox',
			vendor_id: '42',
			backup_path: '/backups/master.db'
		});
	};

	const result = await reconcileApi.applyRelocate('sid-1', '/new/a.flac', {
		ifMatch: '"rev-1"',
		expectedOriginalPath: '/old/a.flac',
		expectedVendorId: '42',
		expectedCandidateIdentity: 'token-9'
	});

	assert.equal(seen.url, `${API_BASE}/api/v1/relocate/sid-1/apply`);
	assert.equal(seen.method, 'POST');
	assert.equal(seen.headers.get('if-match'), '"rev-1"');
	assert.equal(seen.headers.get('content-type'), 'application/json');
	assert.deepEqual(body, {
		new_path: '/new/a.flac',
		expected_candidate_identity: 'token-9',
		expected_original_path: '/old/a.flac',
		expected_vendor_id: '42',
		confirm: true
	});
	assert.equal(result.target, 'rekordbox');
});

test('a failed apply surfaces the route code and message, not a bare status', async () => {
	globalThis.fetch = async () =>
		new Response(
			JSON.stringify({ detail: { code: 'RELOCATE_STALE', message: 'recorded path changed' } }),
			{ status: 409, statusText: 'Conflict', headers: { 'content-type': 'application/json' } }
		);

	const caught = await reconcileApi
		.applyRelocate('sid-1', '/new/a.flac', {
			ifMatch: '"rev-1"',
			expectedOriginalPath: '/old/a.flac',
			expectedVendorId: null,
			expectedCandidateIdentity: 'token-9'
		})
		.then(
			() => null,
			(error) => error
		);

	assert.ok(caught instanceof reconcileApi.RelocateApplyError, 'expected a RelocateApplyError');
	assert.equal(caught.status, 409);
	assert.equal(caught.code, 'RELOCATE_STALE');
	assert.equal(caught.message, 'recorded path changed');
});
