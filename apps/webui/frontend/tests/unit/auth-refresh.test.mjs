/**
 * refreshUser must treat signed-out as identity, not a fault.
 *
 * Regression lines:
 * - if HTTP 200 with signed_in:false leaves auth.error set then the bauble
 *   shows a false failure on every signed-out mount
 * - if HTTP 200 with a user envelope does not populate auth.user then a
 *   signed-in operator looks signed out
 * - if HTTP 401 is treated as an error then older daemons and e2e stubs break
 * - if HTTP 503 is swallowed then a missing OAuth config looks like sign-out
 * - if the signed-out probe is HTTP 401 then Chromium logs console.error
 *   Failed to load resource during AutoPlay/mixing (#1876)
 */

import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SIGNED_IN_USER = {
	google_sub: 'sub-abc',
	email: 'sub-abc@example.com',
	name: 'Test User',
	avatar_url: 'https://lh3.googleusercontent.com/test',
	created_at: '2026-01-01T00:00:00+00:00'
};

let originalFetch;

afterEach(() => {
	globalThis.fetch = originalFetch;
});

function mockFetch(handler) {
	originalFetch = globalThis.fetch;
	globalThis.fetch = handler;
}

test('HTTP 200 signed-out clears user without error', async () => {
	const paths = [];
	mockFetch(async (input) => {
		paths.push(String(input));
		return new Response(JSON.stringify({ signed_in: false, user: null }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	});
	const mod = await loadTypeScriptModule('src/lib/auth.svelte.ts');
	await mod.refreshUser();
	assert.equal(paths.length, 1);
	assert.match(paths[0], /\/api\/v1\/auth\/me$/);
	assert.equal(mod.auth.user, null);
	assert.equal(mod.auth.error, null);
	assert.equal(mod.auth.loading, false);
});

test('HTTP 200 signed-in populates user without error', async () => {
	mockFetch(async () =>
		new Response(JSON.stringify({ signed_in: true, user: SIGNED_IN_USER }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		})
	);
	const mod = await loadTypeScriptModule('src/lib/auth.svelte.ts');
	await mod.refreshUser();
	assert.equal(mod.auth.user.email, SIGNED_IN_USER.email);
	assert.equal(mod.auth.error, null);
	assert.equal(mod.auth.loading, false);
});

test('HTTP 401 is still signed-out without error (compat)', async () => {
	mockFetch(async () =>
		new Response(JSON.stringify({}), {
			status: 401,
			headers: { 'content-type': 'application/json' }
		})
	);
	const mod = await loadTypeScriptModule('src/lib/auth.svelte.ts');
	await mod.refreshUser();
	assert.equal(mod.auth.user, null);
	assert.equal(mod.auth.error, null);
	assert.equal(mod.auth.loading, false);
});

test('HTTP 503 surfaces the daemon error message', async () => {
	mockFetch(async () =>
		new Response(
			JSON.stringify({
				detail: { code: 'AUTH_NOT_CONFIGURED', message: 'OAuth not configured' }
			}),
			{
				status: 503,
				headers: { 'content-type': 'application/json' }
			}
		)
	);
	const mod = await loadTypeScriptModule('src/lib/auth.svelte.ts');
	await mod.refreshUser();
	assert.equal(mod.auth.user, null);
	assert.equal(mod.auth.error, 'OAuth not configured');
	assert.equal(mod.auth.loading, false);
});
