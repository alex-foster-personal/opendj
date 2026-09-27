/**
 * @pytest.mark.requirement UX-TOAST-03
 * [if] five error toasts pushed [then] visible slice length is at most three [else stop].
 */
import assert from 'node:assert/strict';
import { afterEach, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://toast-tray-cap.example.test';

function defineGlobal(name, value) {
	Object.defineProperty(globalThis, name, {
		value,
		configurable: true,
		writable: true,
		enumerable: true
	});
}

function makeLocalStorage() {
	const map = new Map();
	return {
		getItem: (key) => (map.has(key) ? map.get(key) : null),
		setItem: (key, value) => map.set(key, String(value)),
		removeItem: (key) => map.delete(key)
	};
}

let originalFetch;

function install() {
	const store = makeLocalStorage();
	defineGlobal('window', {
		location: { href: 'http://127.0.0.1:8585/performance', pathname: '/performance' },
		isSecureContext: true,
		localStorage: store,
		addEventListener: () => {}
	});
	defineGlobal('localStorage', store);
	defineGlobal('navigator', {
		userAgent: 'Mozilla/5.0',
		clipboard: { writeText: async () => {} }
	});
	defineGlobal('crypto', {
		randomUUID: () => 'uuid-cap-test',
		getRandomValues: (array) => {
			for (let i = 0; i < array.length; i += 1) array[i] = i;
			return array;
		}
	});
	defineGlobal('AudioWorkletNode', function AudioWorkletNode() {});

	originalFetch = globalThis.fetch;
	globalThis.fetch = async (input) => {
		const url = typeof input === 'string' ? input : input.url;
		if (url.includes('/api/v1/settings')) {
			return new Response(
				JSON.stringify({
					groups: [{ group: 'network', items: [{ key: 'hostname', value: 'test-host' }] }]
				}),
				{ status: 200, headers: { 'content-type': 'application/json' } }
			);
		}
		if (url.includes('/api/v1/client-errors')) {
			return new Response(JSON.stringify({ event_id: 'e1', stored: true }), {
				status: 200,
				headers: { 'content-type': 'application/json' }
			});
		}
		return new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } });
	};
}

beforeEach(() => install());

afterEach(() => {
	globalThis.fetch = originalFetch;
	delete globalThis.window;
});

test('five pushed toasts yield at most three visible in policy slice', async (t) => {

	const stores = await loadTypeScriptModule('src/lib/stores.svelte.ts', {
		viteApiBase: API_BASE
	});
	const policy = await loadTypeScriptModule('src/lib/toast-tray-policy.ts');
	for (let i = 0; i < 5; i += 1) {
		stores.pushToast(`burst ${i}`, 'error', 120_000);
	}
	// pushToast(..., 120_000) arms a REAL 120 s dismissal timer per toast
	// (stores.svelte.ts _armTimer). Left armed, the three uncapped toasts held
	// the process open past the suite's 120 s --test-timeout, failing the file
	// after every assertion had passed. Dismiss through the production path so
	// the real handles are cleared the moment the test ends.
	t.after(() => {
		for (const toast of [...stores.toasts]) stores.dismissToast(toast.logId);
		assert.equal(stores.toasts.length, 0, 'every toast dismissed, no timer left armed');
	});
	const nonExiting = stores.toasts.filter((t) => t.exiting !== true);
	assert.ok(nonExiting.length <= 3, `non-exiting count ${nonExiting.length}`);
	const visible = policy.selectVisibleToasts(stores.toasts);
	assert.ok(visible.length <= 3, `visible slice length ${visible.length}`);
});
