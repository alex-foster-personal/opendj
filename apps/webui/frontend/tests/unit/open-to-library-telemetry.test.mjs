// requirement: PERF-UI-03
import assert from 'node:assert/strict';
import { after, afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://telemetry.example.test';
let telemetry;
let originalFetch;
let body;

function installBrowserGlobals(timeOrigin = 1000) {
	Object.defineProperty(globalThis, 'performance', {
		value: { timeOrigin },
		configurable: true,
		writable: true
	});
	Object.defineProperty(globalThis, 'crypto', {
		value: { randomUUID: () => 'boot-span-uuid' },
		configurable: true,
		writable: true
	});
}

before(async () => {
	originalFetch = globalThis.fetch;
	installBrowserGlobals();
	telemetry = await loadTypeScriptModule('src/lib/client-telemetry.ts', { viteApiBase: API_BASE });
});

afterEach(() => {
	body = undefined;
	installBrowserGlobals();
	telemetry.resetOpenToLibraryRowsRecordedForTests();
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('recordOpenToLibraryRows posts open-to-library-rows once using navigationStart', async () => {
	let postCount = 0;
	let resolvePosted;
	const posted = new Promise((resolve) => {
		resolvePosted = resolve;
	});
	globalThis.fetch = async (input) => {
		postCount += 1;
		body = await input.clone().json();
		resolvePosted();
		return new Response(JSON.stringify({ event_id: 'e1', stored: true }), {
			status: 202,
			headers: { 'content-type': 'application/json' }
		});
	};
	telemetry.recordOpenToLibraryRows({ source: 'all-tracks', now: 2500 });
	telemetry.recordOpenToLibraryRows({ source: 'playlist', now: 3000 });
	await posted;
	assert.equal(postCount, 1);
	assert.equal(body.name, 'open-to-library-rows');
	assert.equal(body.duration_ms, 1500);
	assert.equal(body.method, 'navigationStart to first track row first paint');
});
