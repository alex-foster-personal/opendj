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

/**
 * Boot id contract (NATIVE-5886). The next-up resend after an engine restart
 * hangs off this value, so a response that should carry one and does not is
 * a failed probe, never an online engine with an unknown boot.
 *
 * [if] the engine health carries a boot id [then] pingHealth returns it.
 * [if] the body has none of the engine handshake fields (the legacy daemon)
 *   [then] null [else the Backend dot goes red on a daemon that is up].
 * [if] boot_id is missing beside other handshake fields, empty, or not a string
 *   [then] the probe rejects [else contract drift reads as online].
 */
function healthResponse(body) {
	return new Response(JSON.stringify(body), {
		status: 200,
		headers: { 'content-type': 'application/json' }
	});
}

const ENGINE_HEALTH = {
	status: 'ok',
	version: '1',
	contract_rev: 'rev-1',
	engine_version: '1.0.0',
	boot_id: 'boot-1'
};

test('pingHealth returns the engine boot id', async () => {
	globalThis.fetch = async () => healthResponse(ENGINE_HEALTH);
	assert.equal(await api.pingHealth(2000), 'boot-1');
});

test('pingHealth returns null only for a legacy body with no engine handshake', async () => {
	globalThis.fetch = async () => healthResponse({ status: 'ok', version: '1' });
	assert.equal(await api.pingHealth(2000), null);
});

for (const [label, patch] of [
	['missing beside the other handshake fields', { boot_id: undefined }],
	['empty', { boot_id: '' }],
	['a number', { boot_id: 7 }],
	['null', { boot_id: null }]
]) {
	test(`pingHealth rejects a boot_id that is ${label}`, async () => {
		globalThis.fetch = async () => healthResponse({ ...ENGINE_HEALTH, ...patch });
		await assert.rejects(() => api.pingHealth(2000), /boot_id/);
	});
}

test('pingHealth rejects a lone malformed boot_id on an otherwise legacy body', async () => {
	globalThis.fetch = async () => healthResponse({ status: 'ok', version: '1', boot_id: '' });
	await assert.rejects(() => api.pingHealth(2000), /boot_id/);
});
