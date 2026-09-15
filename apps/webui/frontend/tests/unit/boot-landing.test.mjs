/**
 * PERFMODE-11: session-gated cold-open redirect from `/`.
 */
import assert from 'node:assert/strict';
import { afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FAKE_NAVIGATION = new URL('./fake-app-navigation.mjs', import.meta.url).pathname;

let bootLanding;
let gotoCalls;

function installSessionStorage() {
	const store = new Map();
	globalThis.sessionStorage = {
		getItem: (key) => store.get(key) ?? null,
		setItem: (key, value) => store.set(key, String(value)),
		removeItem: (key) => store.delete(key),
		clear: () => store.clear()
	};
	return store;
}

before(async () => {
	bootLanding = await loadTypeScriptModule('src/lib/rb/boot-landing.ts', {
		alias: { '$app/navigation': FAKE_NAVIGATION }
	});
});

afterEach(() => {
	delete globalThis.sessionStorage;
	delete globalThis.__shellNavGotoCalls;
	gotoCalls = undefined;
});

test('first `/` visit redirects to Gig and sets session gate', () => {
	const store = installSessionStorage();
	gotoCalls = [];
	globalThis.__shellNavGotoCalls = gotoCalls;

	bootLanding.installBootLandingRedirect({
		goto: (route, opts) => gotoCalls.push({ route, opts }),
		getPathname: () => '/',
		readBootStamp: () => null,
		nowMs: () => Date.parse('2026-09-15T12:00:00.000Z')
	});

	assert.deepEqual(gotoCalls, [{ route: '/performance', opts: { replaceState: true } }]);
	assert.equal(store.get(bootLanding.BOOT_LANDING_SESSION_KEY), '1');
});

test('second `/` visit in the same session does not redirect', () => {
	const store = installSessionStorage();
	store.set(bootLanding.BOOT_LANDING_SESSION_KEY, '1');
	gotoCalls = [];

	bootLanding.installBootLandingRedirect({
		goto: (route) => gotoCalls.push(route),
		getPathname: () => '/',
		readBootStamp: () => new Date().toISOString()
	});

	assert.deepEqual(gotoCalls, []);
});

test('deep links are untouched', () => {
	installSessionStorage();
	gotoCalls = [];

	bootLanding.installBootLandingRedirect({
		goto: (route) => gotoCalls.push(route),
		getPathname: () => '/admin',
		readBootStamp: () => null
	});

	assert.deepEqual(gotoCalls, []);
});
