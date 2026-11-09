/**
 * Contract tests for library-jobs-store hydrate error handling (issue #6432).
 */
import assert from 'node:assert/strict';
import { after, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://library-jobs.example.test';

let libraryJobsStore;
let originalFetch;
let originalConsoleError;
let errors;

function jsonResponse(body, status = 200) {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json' }
	});
}

before(async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/library-jobs-store.svelte.ts', {
		viteApiBase: API_BASE
	});
	libraryJobsStore = mod.libraryJobsStore;
	originalFetch = globalThis.fetch;
	originalConsoleError = console.error;
});

after(() => {
	globalThis.fetch = originalFetch;
	console.error = originalConsoleError;
});

beforeEach(() => {
	errors = [];
	console.error = (...args) => errors.push(args.map(String).join(' '));
	globalThis.fetch = async (request) => {
		const url = typeof request === 'string' ? request : request.url;
		if (url.includes('/api/v1/library-jobs')) {
			return jsonResponse(
				{ detail: { code: 'list_failed', message: 'Internal Server Error' } },
				500
			);
		}
		throw new Error(`unexpected fetch: ${url}`);
	};
});

test('refresh resolves and logs when listLibraryJobs fails', async () => {
	await libraryJobsStore.refresh();
	assert.ok(
		errors.some((line) => line.includes('[library-jobs-store] hydrate failed')),
		`expected hydrate failure log, got: ${JSON.stringify(errors)}`
	);
});
