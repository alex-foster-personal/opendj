/**
 * Contract tests for library-jobs-store hydrate error handling (issue #6432, #13530).
 */
import assert from 'node:assert/strict';
import { after, afterEach, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://library-jobs.example.test';

let mod;
let originalFetch;
let originalConsoleError;
let errors;

function jsonResponse(body, status = 200) {
	return new Response(JSON.stringify(body), {
		status,
		statusText: status === 500 ? 'Internal Server Error' : 'OK',
		headers: { 'content-type': 'application/json' }
	});
}

before(async () => {
	mod = await loadTypeScriptModule('tests/unit/fixtures/library-jobs-store-entry.ts', {
		viteApiBase: API_BASE
	});
	originalFetch = globalThis.fetch;
	originalConsoleError = console.error;
});

after(() => {
	globalThis.fetch = originalFetch;
	console.error = originalConsoleError;
});

afterEach(() => {
	mod._resetPageGoingAwayForTests();
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

test('refresh resolves and logs when listLibraryJobs fails on a live page', async () => {
	await mod.libraryJobsStore.refresh();
	assert.ok(
		errors.some((line) => line.includes('[library-jobs-store] hydrate failed')),
		`expected hydrate failure log, got: ${JSON.stringify(errors)}`
	);
});

test('refresh stays quiet on unload when listLibraryJobs fails', async () => {
	mod.markPageGoingAwayForTests();
	await mod.libraryJobsStore.refresh();
	assert.equal(errors.length, 0, `unexpected console on unload: ${JSON.stringify(errors)}`);
});
