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
let originalConsole;

function install() {
	const store = makeLocalStorage();
	defineGlobal('window', {
		location: {
			href: 'http://127.0.0.1:8585/performance',
			pathname: '/performance'
		},
		isSecureContext: true,
		localStorage: store,
		addEventListener: () => {}
	});
	defineGlobal('localStorage', store);
	defineGlobal('navigator', {
		userAgent:
			'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15',
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

	originalConsole = { info: console.info, warn: console.warn, error: console.error };
	for (const level of ['info', 'warn', 'error']) {
		console[level] = () => {};
	}

	originalFetch = globalThis.fetch;
	globalThis.fetch = async (input) => {
		const url = typeof input === 'string' ? input : input.url;
		if (url.includes('/api/v1/settings')) {
			return new Response(
				JSON.stringify({
					groups: [
						{
							group: 'network',
							items: [
								{ key: 'bind_host', value: '127.0.0.1' },
								{ key: 'hostname', value: 'test-host' }
							]
						}
					]
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
	if (originalConsole) Object.assign(console, originalConsole);
	delete globalThis.window;
	delete globalThis.localStorage;
	delete globalThis.navigator;
	delete globalThis.crypto;
	delete globalThis.AudioWorkletNode;
});

test('five pushed toasts yield at most three visible in policy slice', async () => {
	const stores = await loadTypeScriptModule('src/lib/stores.svelte.ts', {
		viteApiBase: API_BASE
	});
	const policy = await loadTypeScriptModule('src/lib/toast-tray-policy.ts');
	for (let i = 0; i < 5; i += 1) {
		stores.pushToast(`burst ${i}`, 'error', 120_000);
	}
	const nonExiting = stores.toasts.filter((t) => t.exiting !== true);
	assert.ok(nonExiting.length <= 3, `non-exiting count ${nonExiting.length}`);
	const visible = policy.selectVisibleToasts(stores.toasts);
	assert.ok(visible.length <= 3, `visible slice length ${visible.length}`);
});
