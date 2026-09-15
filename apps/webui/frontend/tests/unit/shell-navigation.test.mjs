/**
 * Shell navigation poll: desktop-shell only, goto + ack (issue #2866).
 */
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FAKE_NAVIGATION = fileURLToPath(new URL('./fake-app-navigation.mjs', import.meta.url));

let shellNavigation;
let fetchCalls;

function installGlobals({ shell = false, pathname = '/' } = {}) {
	globalThis.__shellNavGotoCalls = [];
	fetchCalls = [];
	const win = {
		location: { pathname },
		OPENDJ_ENGINE_ORIGIN: shell ? 'http://127.0.0.1:8685' : undefined
	};
	Object.defineProperty(globalThis, 'window', {
		value: win,
		configurable: true,
		writable: true,
		enumerable: true
	});
	globalThis.fetch = async (url, init) => {
		fetchCalls.push({ url, init });
		if (url.endsWith('/api/v1/shell/navigate/pending')) {
			return {
				ok: true,
				json: async () => ({ pending: { id: 'nav-1', route: '/performance' } })
			};
		}
		if (url.endsWith('/api/v1/shell/navigate/ack')) {
			return { ok: true, json: async () => ({ acknowledged: true }) };
		}
		throw new Error(`unexpected fetch ${url}`);
	};
}

before(async () => {
	shellNavigation = await loadTypeScriptModule('src/lib/rb/shell-navigation.ts', {
		alias: { '$app/navigation': FAKE_NAVIGATION }
	});
});

afterEach(() => {
	delete globalThis.window;
	delete globalThis.fetch;
	delete globalThis.__shellNavGotoCalls;
});

test('installShellNavigationPoll is a no-op in browser tabs', () => {
	installGlobals({ shell: false });
	const teardown = shellNavigation.installShellNavigationPoll();
	teardown();
	assert.equal(fetchCalls.length, 0);
	assert.equal(globalThis.__shellNavGotoCalls?.length ?? 0, 0);
});

test('desktop shell polls pending, navigates, and acks', async () => {
	installGlobals({ shell: true, pathname: '/' });
	const teardown = shellNavigation.installShellNavigationPoll();
	await new Promise((resolve) => setTimeout(resolve, 250));
	teardown();
	assert.ok(fetchCalls.some((call) => call.url.endsWith('/api/v1/shell/navigate/pending')));
	assert.deepEqual(globalThis.__shellNavGotoCalls, ['/performance']);
	assert.ok(
		fetchCalls.some(
			(call) =>
				call.url.endsWith('/api/v1/shell/navigate/ack') &&
				call.init?.method === 'POST' &&
				call.init?.body === JSON.stringify({ id: 'nav-1' })
		)
	);
});

test('already on performance skips goto but still acks', async () => {
	installGlobals({ shell: true, pathname: '/performance' });
	const teardown = shellNavigation.installShellNavigationPoll();
	await new Promise((resolve) => setTimeout(resolve, 250));
	teardown();
	assert.deepEqual(globalThis.__shellNavGotoCalls, []);
	assert.ok(fetchCalls.some((call) => call.url.endsWith('/api/v1/shell/navigate/ack')));
});
