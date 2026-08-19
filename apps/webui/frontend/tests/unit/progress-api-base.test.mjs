import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://progress-api.example.test';
let progressApi;
let originalFetch;
let requestedUrl;

before(async () => {
	progressApi = await loadTypeScriptModule('src/routes/progress-tree/progress-api.ts', {
		viteApiBase: API_BASE
	});
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('fetchProgress uses the shared configured API base', async () => {
	// openapi-fetch calls fetch(request) with one Request object.
	globalThis.fetch = async (request) => {
		requestedUrl = request.url;
		return new Response(
			JSON.stringify({
				meta: { branch: 'master', updated: '2026-07-22', convention: 'test' },
				areas: [],
				file_git: { last_sha: null, last_author: null, last_date: null }
			}),
			{ status: 200, headers: { 'content-type': 'application/json' } }
		);
	};

	const progress = await progressApi.fetchProgress();

	assert.equal(requestedUrl, `${API_BASE}/api/v1/progress`);
	assert.equal(progress.meta.branch, 'master');
});

test('non-2xx and unreachable map onto the route error contract', async () => {
	globalThis.fetch = async () => new Response('boom', { status: 500 });
	await assert.rejects(
		progressApi.fetchProgress(),
		/GET \/api\/v1\/progress failed: 500 boom/
	);

	globalThis.fetch = async () => {
		throw new TypeError('fetch failed');
	};
	await assert.rejects(
		progressApi.fetchProgress(),
		/daemon unreachable at .*\/api\/v1\/progress \(fetch failed\)/
	);
});

test('fetchProgress propagates validator failures without daemon-unreachable wrap', async () => {
	// openapi-fetch calls fetch(request) with one Request object.
	globalThis.fetch = async (request) => {
		requestedUrl = request.url;
		return new Response(JSON.stringify([]), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};

	await assert.rejects(progressApi.fetchProgress(), (error) => {
		assert.match(error.message, /progress: /);
		assert.equal(/daemon unreachable/.test(error.message), false);
		return true;
	});
});
