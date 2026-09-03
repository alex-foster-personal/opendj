/**
 * PARITY-02: rbx-vs-own source toggle client (analysis-source.svelte.ts).
 *
 * Regression lines:
 * - if loadAnalysisSource doesn't GET /api/v1/analysis-source and mirror the
 *   response into the reactive state then broken
 * - if setAnalysisSource doesn't PUT {feature, source} and adopt the
 *   response then broken
 * - if a rejected PUT (unknown feature) throws instead of silently keeping
 *   the old local state then broken -- an agent or the UI must see the
 *   failure, never a switch that looks applied but wasn't
 */
import assert from 'node:assert/strict';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://analysis-source.example.test';

let analysisSource;
let originalFetch;

function jsonResponse(body, init = {}) {
	return new Response(JSON.stringify(body), {
		status: 200,
		...init,
		headers: { 'content-type': 'application/json', ...(init.headers ?? {}) }
	});
}

before(async () => {
	analysisSource = await loadTypeScriptModule('src/lib/rb/analysis-source.svelte.ts', {
		viteApiBase: API_BASE
	});
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

beforeEach(() => {
	analysisSource.analysisSourceState.features = {};
});

test('loadAnalysisSource GETs the daemon selection and mirrors it into state', async () => {
	let seen;
	globalThis.fetch = async (request) => {
		seen = request;
		return jsonResponse({ features: { beatgrid: 'rekordbox' } });
	};

	await analysisSource.loadAnalysisSource();

	assert.equal(seen.url, `${API_BASE}/api/v1/analysis-source`);
	assert.equal(seen.method, 'GET');
	assert.deepEqual(analysisSource.analysisSourceState.features, { beatgrid: 'rekordbox' });
});

test('setAnalysisSource PUTs feature+source and adopts the response', async () => {
	let seen;
	let body;
	globalThis.fetch = async (request) => {
		seen = request;
		body = await request.clone().json();
		return jsonResponse({ features: { beatgrid: 'own' } });
	};

	await analysisSource.setAnalysisSource('beatgrid', 'own');

	assert.equal(seen.url, `${API_BASE}/api/v1/analysis-source`);
	assert.equal(seen.method, 'PUT');
	assert.deepEqual(body, { feature: 'beatgrid', source: 'own' });
	assert.deepEqual(analysisSource.analysisSourceState.features, { beatgrid: 'own' });
});

test('a rejected feature throws and leaves prior state untouched', async () => {
	analysisSource.analysisSourceState.features = { beatgrid: 'rekordbox' };
	globalThis.fetch = async () =>
		new Response(
			JSON.stringify({
				detail: {
					code: 'ANALYSIS_SOURCE_FEATURE_NOT_FOUND',
					message: "'vocals' is not a toggleable feature; known: ['beatgrid']"
				}
			}),
			{ status: 404, statusText: 'Not Found', headers: { 'content-type': 'application/json' } }
		);

	await assert.rejects(() => analysisSource.setAnalysisSource('vocals', 'own'));
	assert.deepEqual(analysisSource.analysisSourceState.features, { beatgrid: 'rekordbox' });
});
