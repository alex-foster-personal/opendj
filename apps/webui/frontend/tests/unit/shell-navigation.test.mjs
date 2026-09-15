/**
 * Shell navigation poll: desktop-shell only, goto + ack (issue #2866),
 * hardened poll rate / visibility / failure logging / move toast (#2879).
 */
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { afterEach, before, mock, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FAKE_NAVIGATION = fileURLToPath(new URL('./fake-app-navigation.mjs', import.meta.url));
const FAKE_STORES = fileURLToPath(new URL('./fake-stores-svelte.mjs', import.meta.url));

let shellNavigation;
let fetchCalls;
let visibilityHandlers;
let fakeDocument;

function installGlobals({ shell = false, pathname = '/', hidden = false } = {}) {
	globalThis.__shellNavGotoCalls = [];
	globalThis.__shellNavToastCalls = [];
	fetchCalls = [];
	visibilityHandlers = [];
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
	fakeDocument = {
		hidden,
		addEventListener: (type, handler) => {
			if (type === 'visibilitychange') visibilityHandlers.push(handler);
		},
		removeEventListener: (type, handler) => {
			if (type !== 'visibilitychange') return;
			visibilityHandlers = visibilityHandlers.filter((entry) => entry !== handler);
		}
	};
	Object.defineProperty(globalThis, 'document', {
		value: fakeDocument,
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

/** Let a tick's chain of `await`s (fetch -> json -> goto -> fetch -> json)
 * settle after a mock-timers tick fires the interval callback synchronously. */
async function flushMicrotasks(times = 8) {
	for (let i = 0; i < times; i += 1) {
		await new Promise((resolve) => setImmediate(resolve));
	}
}

function fireVisibilityChange() {
	for (const handler of visibilityHandlers) handler();
}

before(async () => {
	shellNavigation = await loadTypeScriptModule('src/lib/rb/shell-navigation.ts', {
		alias: { '$app/navigation': FAKE_NAVIGATION, '$lib/stores.svelte': FAKE_STORES }
	});
});

afterEach(() => {
	delete globalThis.window;
	delete globalThis.document;
	delete globalThis.fetch;
	delete globalThis.__shellNavGotoCalls;
	delete globalThis.__shellNavToastCalls;
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

test('a move the poll actually performs shows a toast naming the target route', async () => {
	installGlobals({ shell: true, pathname: '/' });
	const teardown = shellNavigation.installShellNavigationPoll();
	await new Promise((resolve) => setTimeout(resolve, 250));
	teardown();
	assert.equal(globalThis.__shellNavToastCalls.length, 1);
	assert.match(globalThis.__shellNavToastCalls[0].message, /\/performance/);
	assert.equal(globalThis.__shellNavToastCalls[0].groupKey, 'shell-navigate-agent-move');
});

test('the poll interval is at least 5 seconds (issue #2879 acceptance)', () => {
	assert.ok(
		shellNavigation.POLL_MS >= 5_000,
		`POLL_MS must be >=5000ms to keep a 60s idle window at or under 12 requests, got ${shellNavigation.POLL_MS}`
	);
});

test('a hidden window stops polling and visibilitychange resumes it', async () => {
	installGlobals({ shell: true, pathname: '/performance', hidden: false });
	mock.timers.enable({ apis: ['setInterval'] });
	try {
		const teardown = shellNavigation.installShellNavigationPoll();
		await flushMicrotasks();
		const afterMount = fetchCalls.length;
		assert.ok(afterMount > 0, 'the immediate tick at mount must fire');

		fakeDocument.hidden = true;
		fireVisibilityChange();
		mock.timers.tick(shellNavigation.POLL_MS * 4);
		await flushMicrotasks();
		assert.equal(
			fetchCalls.length,
			afterMount,
			'no fetch may happen while document.hidden is true'
		);

		fakeDocument.hidden = false;
		fireVisibilityChange();
		await flushMicrotasks();
		assert.ok(
			fetchCalls.length > afterMount,
			'visibilitychange back to visible must resume polling immediately'
		);

		teardown();
	} finally {
		mock.timers.reset();
	}
});

test('an unreachable engine logs at most one failing-since record per outage, no unhandled rejection', async () => {
	installGlobals({ shell: true, pathname: '/performance' });
	globalThis.fetch = async () => {
		throw new TypeError('fetch failed');
	};
	const originalWarn = console.warn;
	const warnings = [];
	console.warn = (...args) => warnings.push(args);
	let unhandled = null;
	const onUnhandled = (reason) => {
		unhandled = reason;
	};
	process.once('unhandledRejection', onUnhandled);
	mock.timers.enable({ apis: ['setInterval'] });
	try {
		const teardown = shellNavigation.installShellNavigationPoll();
		await flushMicrotasks();
		mock.timers.tick(shellNavigation.POLL_MS);
		await flushMicrotasks();
		mock.timers.tick(shellNavigation.POLL_MS);
		await flushMicrotasks();
		teardown();

		assert.equal(warnings.length, 1, 'one outage must produce exactly one warning, not one per tick');
		assert.match(warnings[0][0], /shell navigation poll failing since/);
		assert.equal(unhandled, null, 'a caught engine failure must never surface as an unhandled rejection');
	} finally {
		mock.timers.reset();
		console.warn = originalWarn;
		process.removeListener('unhandledRejection', onUnhandled);
	}
});

test('recovery after an outage re-arms the failing-since record for the NEXT outage', async () => {
	installGlobals({ shell: true, pathname: '/performance' });
	let failing = true;
	globalThis.fetch = async (url) => {
		if (failing) throw new TypeError('fetch failed');
		if (url.endsWith('/api/v1/shell/navigate/pending')) {
			return { ok: true, json: async () => ({ pending: null }) };
		}
		throw new Error(`unexpected fetch ${url}`);
	};
	const originalWarn = console.warn;
	const warnings = [];
	console.warn = (...args) => warnings.push(args);
	mock.timers.enable({ apis: ['setInterval'] });
	try {
		const teardown = shellNavigation.installShellNavigationPoll();
		await flushMicrotasks();
		assert.equal(warnings.length, 1, 'first outage logs once');

		failing = false;
		mock.timers.tick(shellNavigation.POLL_MS);
		await flushMicrotasks();

		failing = true;
		mock.timers.tick(shellNavigation.POLL_MS);
		await flushMicrotasks();
		mock.timers.tick(shellNavigation.POLL_MS);
		await flushMicrotasks();
		teardown();

		assert.equal(warnings.length, 2, 'a second, distinct outage must log its own record');
	} finally {
		mock.timers.reset();
		console.warn = originalWarn;
	}
});

test('already on performance skips goto but still acks', async () => {
	installGlobals({ shell: true, pathname: '/performance' });
	const teardown = shellNavigation.installShellNavigationPoll();
	await new Promise((resolve) => setTimeout(resolve, 250));
	teardown();
	assert.deepEqual(globalThis.__shellNavGotoCalls, []);
	assert.ok(fetchCalls.some((call) => call.url.endsWith('/api/v1/shell/navigate/ack')));
	assert.equal(
		globalThis.__shellNavToastCalls.length,
		0,
		'no toast when the window was already on the target route'
	);
});
