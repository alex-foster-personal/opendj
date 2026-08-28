import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://beatgrid-fallback-cache.example.test';

let cache;
const originalFetch = globalThis.fetch;

before(async () => {
	cache = await loadTypeScriptModule('src/lib/components/rb/wave/beatgrid-fallback-cache.svelte.ts', {
		viteApiBase: API_BASE
	});
});

function jsonResponse(body, init = {}) {
	return new Response(JSON.stringify(body), {
		status: 200,
		...init,
		headers: { 'content-type': 'application/json', ...(init.headers ?? {}) }
	});
}

test('ensureBeatgridFallback fetches once and caches the ready payload', async () => {
	let calls = 0;
	globalThis.fetch = async () => {
		calls += 1;
		return jsonResponse({
			stable_id: 'abc',
			source: 'apps.analysis',
			backend: 'librosa+madmom',
			backend_version: 'librosa==0.10.0',
			bpm: 128,
			bpm_confidence: 0.9,
			anlz_available: false,
			beatgrid: { beat_count: 1, beats: [{ n: 1, bpm: 128, t: 0 }] }
		});
	};
	try {
		assert.equal(cache.getBeatgridFallbackEntry('abc'), undefined);
		cache.ensureBeatgridFallback('abc');
		assert.deepEqual(cache.getBeatgridFallbackEntry('abc'), { status: 'loading' });

		// A second call before the fetch resolves must not fire a second fetch.
		cache.ensureBeatgridFallback('abc');
		await new Promise((resolve) => setTimeout(resolve, 0));

		assert.equal(calls, 1);
		const entry = cache.getBeatgridFallbackEntry('abc');
		assert.equal(entry.status, 'ready');
		assert.equal(entry.data.bpm, 128);
	} finally {
		globalThis.fetch = originalFetch;
	}
});

test('BEATGRID_FALLBACK_NOT_FOUND (a grid is never invented) surfaces as an explicit error code', async () => {
	globalThis.fetch = async () =>
		new Response(
			JSON.stringify({
				detail: {
					code: 'BEATGRID_FALLBACK_NOT_FOUND',
					message: 'no apps.analysis record for stable_id missing-track'
				}
			}),
			{ status: 404, statusText: 'Not Found', headers: { 'content-type': 'application/json' } }
		);
	try {
		cache.ensureBeatgridFallback('missing-track');
		await new Promise((resolve) => setTimeout(resolve, 0));
		assert.deepEqual(cache.getBeatgridFallbackEntry('missing-track'), {
			status: 'error',
			code: 'BEATGRID_FALLBACK_NOT_FOUND'
		});
	} finally {
		globalThis.fetch = originalFetch;
	}
});
