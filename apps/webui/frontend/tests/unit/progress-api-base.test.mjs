import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://progress-api.example.test';
let progressApi;
let originalFetch;
let requestedUrl;

before(async () => {
	// Loaded through the shared-graph entry so the fetcher and the capability
	// probe it consults live in one bundle. The engine and legacy daemons both
	// serve progress once identified; the not-yet-identified case belongs to
	// daemon-capabilities.test.mjs.
	progressApi = await loadTypeScriptModule('tests/unit/fixtures/daemon-capability-entry.ts', {
		viteApiBase: API_BASE
	});
	originalFetch = globalThis.fetch;
	globalThis.fetch = async () =>
		new Response(
			JSON.stringify({
				status: 'ok',
				state_db: {
					path: 'data/state/state.db',
					tracks: 1,
					playlists: 1,
					pairings: 0,
					last_writer_hostname: null,
					last_writer_at: null
				},
				cloud: { lock_holder: null },
				syncthing: null,
				bind_host: '127.0.0.1',
				version: '0.1.0'
			}),
			{ status: 200, headers: { 'content-type': 'application/json' } }
		);
	assert.equal(await progressApi.capabilities.probe(), 'legacy');
	globalThis.fetch = originalFetch;
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
