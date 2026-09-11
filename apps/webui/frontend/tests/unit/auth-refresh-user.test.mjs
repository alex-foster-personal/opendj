/**
 * AutoPlay hunt #1876: Chromium logs every non-2xx fetch as
 * `Failed to load resource: 401 (Unauthorized)`. GET /api/v1/auth/me answers
 * 401 for a signed-out daemon, which is the documented signed-out contract
 * (#1875 owns that status). The bauble still probes who-am-I on /performance
 * during mixing, so the hunt records console.error 401.
 *
 * GET /api/v1/account already answers HTTP 200 with signed_in: false when
 * nobody is signed in (ACCT-01). The mixing-time who-am-I probe must use that
 * 200 path so Chromium has nothing 401 to log. Do not change /auth/me.
 *
 * Regression lines:
 * - if refreshUser GETs /api/v1/auth/me while establishing signed-out identity
 *   then Chromium logs console.error 401 during AutoPlay/mixing
 * - if a signed-out /account 200 leaves auth.error set then signed-out is
 *   treated as a failure
 * - if a signed-in /account 200 does not populate auth.user then the bauble
 *   stays signed-out after a real session
 */
import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

import { loadRuneModule } from './load-rune-module.mjs';

const ENTRY = `
	export { auth, refreshUser } from '$lib/auth.svelte.ts';
`;

const originalFetch = globalThis.fetch;
const fetches = [];

function jsonResponse(status, body) {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'content-type': 'application/json' }
	});
}

function installFetch(handler) {
	fetches.length = 0;
	globalThis.fetch = async (input, init = {}) => {
		const url = String(input);
		fetches.push({ url, method: (init.method ?? 'GET').toUpperCase() });
		return handler(url, init);
	};
}

afterEach(() => {
	globalThis.fetch = originalFetch;
	fetches.length = 0;
});

function pathOf(url) {
	try {
		return new URL(url).pathname;
	} catch {
		return url;
	}
}

test('refreshUser does not GET /auth/me when the daemon is signed out', async () => {
	installFetch((url) => {
		const path = pathOf(url);
		if (path === '/api/v1/account') {
			return jsonResponse(200, { signed_in: false, user: null });
		}
		if (path === '/api/v1/auth/me') {
			return jsonResponse(401, {
				detail: { code: 'AUTH_REQUIRED', message: 'not signed in' }
			});
		}
		return jsonResponse(500, { detail: `unexpected fetch ${path}` });
	});

	const { auth, refreshUser } = await loadRuneModule(ENTRY);
	await refreshUser();

	const paths = fetches.map((row) => pathOf(row.url));
	assert.deepEqual(
		paths,
		['/api/v1/account'],
		'a signed-out who-am-I must not probe /auth/me: Chromium logs that 401 as console.error'
	);
	assert.equal(auth.user, null);
	assert.equal(auth.error, null);
	assert.equal(auth.loading, false);
});

test('refreshUser populates auth.user from a signed-in /account 200', async () => {
	installFetch((url) => {
		const path = pathOf(url);
		if (path === '/api/v1/account') {
			return jsonResponse(200, {
				signed_in: true,
				user: {
					google_sub: 'sub-123',
					email: 'dj@example.test',
					name: 'DJ',
					avatar_url: 'https://lh3.googleusercontent.com/test',
					created_at: '2026-09-11T00:00:00+00:00'
				}
			});
		}
		return jsonResponse(500, { detail: `unexpected fetch ${path}` });
	});

	const { auth, refreshUser } = await loadRuneModule(ENTRY);
	await refreshUser();

	assert.equal(fetches.some((row) => pathOf(row.url).includes('/auth/me')), false);
	assert.equal(auth.user?.email, 'dj@example.test');
	assert.equal(auth.user?.google_sub, 'sub-123');
	assert.equal(auth.error, null);
});
