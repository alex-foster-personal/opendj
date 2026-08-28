/**
 * Wire-shape regression for beatgrid-fallback-api after conversion onto the
 * generated OpenAPI client. openapi-fetch calls fetch(request) with one Request.
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://beatgrid-fallback-api.example.test';

let api;
let originalFetch;

function jsonResponse(body, init = {}) {
	return new Response(JSON.stringify(body), {
		status: 200,
		...init,
		headers: { 'content-type': 'application/json', ...(init.headers ?? {}) }
	});
}

const payload = {
	stable_id: 'sid/1',
	source: 'fallback',
	backend: 'madmom',
	backend_version: '1.0',
	bpm: 128,
	bpm_confidence: 0.9,
	anlz_available: false,
	beatgrid: { beat_count: 1, beats: [{ n: 1, bpm: 128, t: 0 }] }
};

before(async () => {
	api = await loadTypeScriptModule('src/lib/rb/beatgrid-fallback-api.ts', { viteApiBase: API_BASE });
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('fetchBeatgridFallback encodes the stable id and returns the body as-is', async () => {
	let seen;
	globalThis.fetch = async (request) => {
		seen = request;
		return jsonResponse(payload);
	};

	const found = await api.fetchBeatgridFallback('sid/1');

	assert.equal(seen.url, `${API_BASE}/api/v1/tracks/sid%2F1/beatgrid-fallback`);
	assert.equal(seen.method, 'GET');
	assert.deepEqual(found, payload);
});

test('fetchBeatgridFallback maps ANALYSIS_NOT_FOUND onto RbApiError', async () => {
	globalThis.fetch = async () =>
		new Response(
			JSON.stringify({ detail: { code: 'ANALYSIS_NOT_FOUND', message: 'no analysis for track' } }),
			{ status: 404, statusText: 'Not Found', headers: { 'content-type': 'application/json' } }
		);

	const caught = await api.fetchBeatgridFallback('sid-missing').then(
		() => null,
		(error) => error
	);

	assert.ok(caught instanceof Error);
	assert.equal(caught.status, 404);
	assert.equal(caught.code, 'ANALYSIS_NOT_FOUND');
	assert.equal(caught.message, 'ANALYSIS_NOT_FOUND: no analysis for track');
});
