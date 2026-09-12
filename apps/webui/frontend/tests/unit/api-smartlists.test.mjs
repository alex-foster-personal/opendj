/**
 * Wire-shape regression for src/lib/rb/api-smartlists.ts after its conversion
 * onto the generated OpenAPI client (NOT the older smartlists surface in
 * src/lib/api.ts, which tests/unit/smartlists-api.test.mjs covers). URL,
 * id encoding, the optional limit query and the RbApiError mapping must be
 * byte-identical to the hand-rolled fetch it replaced.
 *
 * openapi-fetch calls `fetch(request)` with a single Request object, so
 * assertions read `request.url` / `request.method`.
 *
 * RbApiError instanceof cannot be asserted here: the loader bundles each
 * entry point separately, so this module's RbApiError is a different class
 * object from one imported through another bundle. The error's name and
 * {status, code, message} fields are the contract, so those are asserted.
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://api-smartlists.example.test';

let smartlists;
let originalFetch;

function summaryPayload() {
	return {
		id: 'sl / one',
		name: 'Peak time',
		rule: { field: 'rating', op: '>=', value: 4 },
		rule_summary: 'rating >= 4',
		order_by: 'bpm asc',
		referenced_fields: ['rating'],
		rule_schema_version: 1,
		last_evaluated_at: null,
		created_at: '2026-08-01T00:00:00Z',
		modified_at: '2026-08-01T00:00:01Z'
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
	smartlists = await loadTypeScriptModule('src/lib/rb/api-smartlists.ts', {
		viteApiBase: API_BASE
	});
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('listSmartlists hits the collection route', async () => {
	let seen;
	globalThis.fetch = async (request) => {
		seen = request;
		return jsonResponse([summaryPayload()]);
	};

	const rows = await smartlists.listSmartlists();

	assert.equal(seen.url, `${API_BASE}/api/v1/smartlists`);
	assert.equal(seen.method, 'GET');
	assert.equal(rows[0].id, 'sl / one');
});

test('getSmartlist encodes the id', async () => {
	let seen;
	globalThis.fetch = async (request) => {
		seen = request;
		return jsonResponse(summaryPayload());
	};

	const row = await smartlists.getSmartlist('sl / one');

	assert.equal(seen.url, `${API_BASE}/api/v1/smartlists/sl%20%2F%20one`);
	assert.equal(row.rule_summary, 'rating >= 4');
});

test('getSmartlistTracks sends the limit when given', async () => {
	let seen;
	globalThis.fetch = async (request) => {
		seen = request;
		return jsonResponse({
			smartlist_id: 'sl-1',
			name: 'Peak time',
			rule_summary: 'rating >= 4',
			order_by: 'bpm asc',
			items: ['t-1'],
			tracks: []
		});
	};

	const result = await smartlists.getSmartlistTracks('sl-1', 50);

	assert.equal(seen.url, `${API_BASE}/api/v1/smartlists/sl-1/tracks?limit=50`);
	assert.deepEqual(result.items, ['t-1']);
});

test('getSmartlistTracks omits the query string when limit is not given', async () => {
	let seen;
	globalThis.fetch = async (request) => {
		seen = request;
		return jsonResponse({
			smartlist_id: 'sl-1',
			name: 'Peak time',
			rule_summary: 'rating >= 4',
			order_by: 'bpm asc',
			items: [],
			tracks: []
		});
	};

	await smartlists.getSmartlistTracks('sl-1');

	assert.equal(seen.url, `${API_BASE}/api/v1/smartlists/sl-1/tracks`);
});

test('deleteSmartlist sends DELETE and accepts the bodyless 204', async () => {
	let seen;
	globalThis.fetch = async (request) => {
		seen = request;
		return new Response(null, { status: 204 });
	};

	await smartlists.deleteSmartlist('sl / one');

	assert.equal(seen.url, `${API_BASE}/api/v1/smartlists/sl%20%2F%20one`);
	assert.equal(seen.method, 'DELETE');
});

test('deleteSmartlist maps SMARTLIST_NOT_FOUND to RbApiError', async () => {
	globalThis.fetch = async () =>
		jsonResponse(
			{ detail: { code: 'SMARTLIST_NOT_FOUND', message: 'smartlist not found: nope' } },
			{ status: 404, statusText: 'Not Found' }
		);

	const caught = await smartlists.deleteSmartlist('nope').then(
		() => null,
		(error) => error
	);

	assert.equal(caught.name, 'RbApiError');
	assert.equal(caught.status, 404);
	assert.equal(caught.code, 'SMARTLIST_NOT_FOUND');
});

test('a daemon error keeps the RbApiError contract with the route code and message', async () => {
	globalThis.fetch = async () =>
		jsonResponse(
			{ detail: { code: 'SMARTLISTS_DB_UNAVAILABLE', message: 'state.db is missing' } },
			{ status: 503, statusText: 'Service Unavailable' }
		);

	const caught = await smartlists.listSmartlists().then(
		() => null,
		(error) => error
	);

	assert.equal(caught.name, 'RbApiError');
	assert.equal(caught.status, 503);
	assert.equal(caught.code, 'SMARTLISTS_DB_UNAVAILABLE');
	assert.equal(caught.message, 'SMARTLISTS_DB_UNAVAILABLE: state.db is missing');
});
