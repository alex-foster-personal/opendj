/**
 * startLogin return_to wiring and OAuth callback error consumption.
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FRONTEND_ROOT = join(__dirname, '..', '..');

let originalFetch;
let originalWindow;
let replaceStateCalls;

afterEach(() => {
	globalThis.fetch = originalFetch;
	if (originalWindow !== undefined) {
		globalThis.window = originalWindow;
	}
	replaceStateCalls = [];
});

function mockLocation({
	origin = 'http://127.0.0.1:9418',
	pathname = '/performance',
	search = ''
} = {}) {
	originalWindow = globalThis.window;
	replaceStateCalls = [];
	const location = { origin, pathname, search, hash: '' };
	const history = {
		state: null,
		replaceState: (...args) => {
			replaceStateCalls.push(args);
		}
	};
	globalThis.window = { location, history };
}

function mockFetch(handler) {
	originalFetch = globalThis.fetch;
	globalThis.fetch = handler;
}

test('startLogin POSTs origin and return_to from window.location', async () => {
	mockLocation({ origin: 'http://127.0.0.1:9418', pathname: '/performance' });
	let postedBody;
	mockFetch(async (_input, init) => {
		postedBody = JSON.parse(init.body);
		return new Response(
			JSON.stringify({
				authorization_url: 'https://accounts.google.com/o/oauth2/v2/auth?state=abc',
				state: 'abc',
				redirect_uri: 'http://127.0.0.1:9418/api/v1/auth/callback'
			}),
			{ status: 200, headers: { 'content-type': 'application/json' } }
		);
	});
	const mod = await loadTypeScriptModule('src/lib/auth.svelte.ts');
	const result = await mod.startLogin();
	assert.deepEqual(postedBody, {
		origin: 'http://127.0.0.1:9418',
		return_to: '/performance'
	});
	assert.match(result.authorization_url, /^https:\/\/accounts\.google\.com/);
});

test('startLogin on 503 throws Error with the runbook message', async () => {
	mockLocation();
	const runbook = 'OAuth not configured: set OPENDJ_GOOGLE_OAUTH_CLIENT_ID';
	mockFetch(async () =>
		new Response(
			JSON.stringify({
				detail: { code: 'AUTH_NOT_CONFIGURED', message: runbook }
			}),
			{ status: 503, headers: { 'content-type': 'application/json' } }
		)
	);
	const mod = await loadTypeScriptModule('src/lib/auth.svelte.ts');
	await assert.rejects(
		() => mod.startLogin(),
		(err) => err instanceof Error && err.message === runbook
	);
});

test('consumeAuthErrorFromLocation returns the param and strips it', async () => {
	mockLocation({
		pathname: '/performance',
		search: '?opendj_auth_error=Google%20refused%20the%20sign-in%3A%20access_denied&foo=bar'
	});
	const mod = await loadTypeScriptModule('src/lib/auth.svelte.ts');
	const message = mod.consumeAuthErrorFromLocation();
	assert.equal(message, 'Google refused the sign-in: access_denied');
	assert.equal(replaceStateCalls.length, 1);
	assert.equal(replaceStateCalls[0][2], '/performance?foo=bar');
});

test('consumeAuthErrorFromLocation returns null when absent', async () => {
	mockLocation({ search: '' });
	const mod = await loadTypeScriptModule('src/lib/auth.svelte.ts');
	assert.equal(mod.consumeAuthErrorFromLocation(), null);
	assert.equal(replaceStateCalls.length, 0);
});

test('UserBauble signed-out button does not disable on auth.loading', () => {
	const source = readFileSync(
		join(FRONTEND_ROOT, 'src/lib/components/UserBauble.svelte'),
		'utf8'
	);
	// The button's own disabled attribute: the sign-out menu item carries its own
	// `disabled={busy}` further down the file, so scope the match to the one that
	// starts with `busy ||`.
	const disabled = source.match(/disabled=\{busy \|\|([^}]*)\}/)?.[1] ?? '';
	// auth.loading may only bite once a user is present: a signed-out visitor has
	// to be able to click while the first /api/v1/auth/me is still in flight.
	// Counting occurrences is what makes this bite - a third term re-introducing
	// an unguarded `auth.loading` would be a second occurrence.
	assert.equal(
		(disabled.match(/auth\.loading/g) ?? []).length,
		1,
		'auth.loading is present and guarded once'
	);
	assert.match(disabled, /\(Boolean\(auth\.user\) && auth\.loading\)/);
	assert.match(disabled, /signInUnavailable/);
	assert.doesNotMatch(source, /disabled=\{busy \|\| auth\.loading\}/);
});
