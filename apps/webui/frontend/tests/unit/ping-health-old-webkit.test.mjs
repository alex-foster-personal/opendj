/**
 * Sol review thread 3966717870 on PR #1560 (P2, non-blocking, fixed anyway):
 * `AbortSignal.timeout` is unavailable on WebKit releases the desktop app's
 * own `minimumSystemVersion` ("11.0", `apps/desktop/src-tauri/tauri.conf.json`)
 * still permits - macOS 11 Big Sur tops out at Safari/WebKit 15.x, and
 * `AbortSignal.timeout` needs Safari 16. Calling it there throws
 * synchronously ("AbortSignal.timeout is not a function"), before the probe
 * ever issues its fetch, so `pingHealth` - restored by this PR to feed the
 * Backend liveness dot - would permanently report the backend offline on a
 * supported install even though it is perfectly healthy.
 *
 * This is the ONLY caller of `AbortSignal.timeout` inside `src/lib/api.ts`
 * (the other, `BrowserPanel.svelte`'s `_pingFrontend`, has its own test:
 * `ping-frontend-old-webkit.test.mjs`).
 *
 * [if] `AbortSignal.timeout` is unavailable (old WebKit) [then ⛔️] pingHealth
 * must still issue its fetch and resolve normally on a 2xx response, not
 * throw before the network call ever happens.
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://ping-health.example.test';

let api;
let originalFetch;
let originalAbortSignalTimeout;

before(async () => {
	api = await loadTypeScriptModule('src/lib/api.ts', { viteApiBase: API_BASE });
	originalFetch = globalThis.fetch;
	originalAbortSignalTimeout = AbortSignal.timeout;
});

after(() => {
	globalThis.fetch = originalFetch;
	AbortSignal.timeout = originalAbortSignalTimeout;
});

test('pingHealth still issues its fetch and resolves when AbortSignal.timeout is unavailable (old WebKit)', async () => {
	// Old WebKit does not define AbortSignal.timeout at all; calling it
	// throws exactly as an undefined property access-as-call would.
	AbortSignal.timeout = undefined;

	let fetchCalled = false;
	globalThis.fetch = async (request) => {
		fetchCalled = true;
		assert.ok(request.signal instanceof AbortSignal, 'the request must still carry a real AbortSignal');
		return new Response(JSON.stringify({ status: 'ok', version: '1' }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};

	await assert.doesNotReject(
		() => api.pingHealth(2000),
		'pingHealth must not throw when AbortSignal.timeout is missing - it must fall back to AbortController + setTimeout'
	);
	assert.ok(fetchCalled, 'pingHealth must have actually issued its fetch, not just swallowed the error');
});

test('pingHealth still throws on a genuine non-2xx (the timeout fallback must not mask real failures)', async () => {
	AbortSignal.timeout = undefined;
	globalThis.fetch = async () =>
		new Response(JSON.stringify({ detail: 'engine down' }), {
			status: 503,
			headers: { 'content-type': 'application/json' }
		});

	await assert.rejects(() => api.pingHealth(2000));
});
