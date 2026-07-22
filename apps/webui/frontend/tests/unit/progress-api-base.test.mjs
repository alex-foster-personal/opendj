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
	globalThis.fetch = async (input) => {
		requestedUrl = String(input);
		return new Response(
			JSON.stringify({
				meta: { branch: 'master', updated: '2026-07-22', convention: 'test' },
				areas: [],
				file_git: { last_sha: null, last_author: null, last_date: null }
			}),
			{ status: 200, headers: { 'content-type': 'application/json' } }
		);
	};
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('fetchProgress uses the shared configured API base', async () => {
	const progress = await progressApi.fetchProgress();

	assert.equal(requestedUrl, `${API_BASE}/api/v1/progress`);
	assert.equal(progress.meta.branch, 'master');
});
