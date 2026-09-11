/**
 * Wire-shape regression for auto-cues-api: GET /tracks/{sid}/auto-cues via
 * the generated OpenAPI client, not api-rb.ts.
 *
 * Regression lines:
 * - if /auto-cues is fetched through api-rb.ts then the hotspot rule is broken
 * - if the stable id is not URL-encoded then a slash in sid splits the path
 * - if 404 ANALYSIS_NOT_FOUND is not mapped onto RbApiError then missing
 *   analysis is treated as a hard error
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://auto-cues-api.example.test';
const SRC = fileURLToPath(new URL('../../src', import.meta.url));

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
	proposal: true,
	source: 'apps.analysis',
	backend: 'librosa',
	backend_version: '1.0',
	proposals: [
		{ time_s: 12.4, kind: 'intro', confidence: 0.9, source: 'heuristic', rms_dbfs: -12 }
	]
};

before(async () => {
	api = await loadTypeScriptModule('src/lib/rb/auto-cues-api.ts', { viteApiBase: API_BASE });
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('auto-cues-api is a dedicated consumer and does not fetch through api-rb.ts', () => {
	const text = readFileSync(`${SRC}/lib/rb/auto-cues-api.ts`, 'utf8');
	assert.match(
		text,
		/api\.GET\('\/api\/v1\/tracks\/\{stable_id\}\/auto-cues'/,
		'auto-cues-api must GET /api/v1/tracks/{stable_id}/auto-cues through the generated client'
	);
	assert.ok(
		text.includes("from './api-rb'") || text.includes("from '$lib/rb/api-rb'"),
		'RbApiError is imported from api-rb; the GET itself must not live there'
	);
	assert.match(text, /api\.GET\(/);
	assert.doesNotMatch(text, /fetchHotCues/);

	const rb = readFileSync(`${SRC}/lib/rb/api-rb.ts`, 'utf8');
	assert.doesNotMatch(
		rb,
		/auto-cues/,
		'api-rb.ts must not grow an auto-cues fetch (analysis-router consumer owns its own wrapper)'
	);
});

test('fetchAutoCues encodes the stable id and returns the body as-is', async () => {
	let seen;
	globalThis.fetch = async (request) => {
		seen = request;
		return jsonResponse(payload);
	};

	const found = await api.fetchAutoCues('sid/1');

	assert.equal(seen.url, `${API_BASE}/api/v1/tracks/sid%2F1/auto-cues`);
	assert.equal(seen.method, 'GET');
	assert.deepEqual(found, payload);
});

test('fetchAutoCues maps ANALYSIS_NOT_FOUND onto RbApiError', async () => {
	globalThis.fetch = async () =>
		new Response(
			JSON.stringify({ detail: { code: 'ANALYSIS_NOT_FOUND', message: 'no analysis for track' } }),
			{ status: 404, statusText: 'Not Found', headers: { 'content-type': 'application/json' } }
		);

	const caught = await api.fetchAutoCues('sid-missing').then(
		() => null,
		(error) => error
	);

	assert.ok(caught instanceof Error);
	assert.equal(caught.status, 404);
	assert.equal(caught.code, 'ANALYSIS_NOT_FOUND');
	assert.equal(caught.message, 'ANALYSIS_NOT_FOUND: no analysis for track');
});
