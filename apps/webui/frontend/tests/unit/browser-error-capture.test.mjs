/**
 * Issue #1714: browser error classes that never reached reportClientError.
 */
import assert from 'node:assert/strict';
import { after, afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://browser-capture.example.test';
const QUEUE_KEY = 'music-dj-tools:client-errors:v1';

let reporting;
let originalFetch;
let store;
let listeners;
let addEventListenerImpl;

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
		setItem: (key, value) => {
			map.set(key, String(value));
		},
		removeItem: (key) => {
			map.delete(key);
		},
		_map: map
	};
}

function installBrowserGlobals() {
	store = makeLocalStorage();
	listeners = new Map();
	addEventListenerImpl = (type, handler, options) => {
		const capture = options === true || options?.capture === true;
		const key = `${type}:${capture ? 'capture' : 'bubble'}`;
		const bucket = listeners.get(key) ?? [];
		bucket.push(handler);
		listeners.set(key, bucket);
	};
	defineGlobal('window', {
		location: { href: 'https://app.example.test/performance' },
		isSecureContext: true,
		localStorage: store,
		addEventListener: addEventListenerImpl
	});
	defineGlobal('localStorage', store);
	defineGlobal('navigator', { userAgent: 'browser-capture-test-agent' });
	defineGlobal('crypto', { randomUUID: () => '11111111-2222-3333-4444-555555555555' });
	defineGlobal('AudioWorkletNode', function AudioWorkletNode() {});
	defineGlobal('console', {
		error: (...args) => {
			globalThis.__consoleErrorCalls = (globalThis.__consoleErrorCalls ?? 0) + 1;
			globalThis.__lastConsoleError = args;
		},
		warn: (...args) => {
			globalThis.__consoleWarnCalls = (globalThis.__consoleWarnCalls ?? 0) + 1;
			globalThis.__lastConsoleWarn = args;
		}
	});
}

function fire(type, event, capture = false) {
	const key = `${type}:${capture ? 'capture' : 'bubble'}`;
	for (const handler of listeners.get(key) ?? []) {
		handler(event);
	}
}

before(async () => {
	originalFetch = globalThis.fetch;
	installBrowserGlobals();
	reporting = await loadTypeScriptModule('src/lib/client-error-reporting.ts', {
		viteApiBase: API_BASE
	});
	reporting.__setConsoleSampleGateForTests(() => 0);
});

afterEach(() => {
	installBrowserGlobals();
	reporting.__resetClientErrorReportingForTests();
	reporting.__setConsoleSampleGateForTests(() => 0);
	globalThis.__consoleErrorCalls = 0;
	globalThis.__consoleWarnCalls = 0;
});

after(() => {
	globalThis.fetch = originalFetch;
});

async function waitFor(predicate, label, timeoutMs = 5000) {
	const deadline = Date.now() + timeoutMs;
	for (;;) {
		if (predicate()) return;
		if (Date.now() > deadline) throw new Error(`timed out waiting for ${label}`);
		await new Promise((resolve) => setTimeout(resolve, 5));
	}
}

test('installClientErrorReporting registers capture-phase resource errors', () => {
	reporting.installClientErrorReporting();
	assert.ok(
		listeners.has('error:capture'),
		'resource load failures require capture:true on the window error listener'
	);
});

test('a resource load failure is reported as resource-error', async () => {
	const posted = [];
	globalThis.fetch = async (input) => {
		posted.push(await input.clone().json());
		return new Response(JSON.stringify({ event_id: 'e1', stored: true }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};
	reporting.installClientErrorReporting();
	const target = { tagName: 'SCRIPT', src: 'https://app.example.test/missing.js' };
	fire(
		'error',
		{
			target,
			message: 'Script error.',
			filename: 'https://app.example.test/missing.js',
			lineno: 1,
			colno: 1
		},
		true
	);
	await waitFor(() => posted.length === 1, 'resource-error POST');
	assert.equal(posted[0].kind, 'resource-error');
	assert.equal(posted[0].context.source, 'resource-error');
	assert.equal(posted[0].context.tag, 'SCRIPT');
});

test('a CSP violation is reported as csp-violation', async () => {
	const posted = [];
	globalThis.fetch = async (input) => {
		posted.push(await input.clone().json());
		return new Response(JSON.stringify({ event_id: 'e2', stored: true }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};
	reporting.installClientErrorReporting();
	fire('securitypolicyviolation', {
		violatedDirective: 'script-src',
		effectiveDirective: 'script-src',
		blockedURI: 'https://evil.example.test/payload.js',
		documentURI: 'https://app.example.test/performance',
		originalPolicy: "script-src 'self'"
	});
	await waitFor(() => posted.length === 1, 'csp-violation POST');
	assert.equal(posted[0].kind, 'csp-violation');
	assert.equal(posted[0].context.source, 'csp-violation');
	assert.match(posted[0].message, /script-src/);
});

test('console.error is sampled and forwarded as console-error', async () => {
	const posted = [];
	globalThis.fetch = async (input) => {
		posted.push(await input.clone().json());
		return new Response(JSON.stringify({ event_id: 'e3', stored: true }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};
	reporting.__setConsoleSampleGateForTests(() => 0);
	reporting.installClientErrorReporting();
	console.error('deck load probe', { deck: 2 });
	await waitFor(() => posted.length === 1, 'console-error POST');
	assert.equal(posted[0].kind, 'console-error');
	assert.match(posted[0].message, /deck load probe/);
});

test('console.warn respects the sample gate', async () => {
	const posted = [];
	globalThis.fetch = async (input) => {
		posted.push(await input.clone().json());
		return new Response(JSON.stringify({ event_id: 'e4', stored: true }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};
	reporting.__setConsoleSampleGateForTests(() => 0.99);
	reporting.installClientErrorReporting();
	console.warn('ignored warn');
	await waitFor(() => posted.length === 0, 'sampled-out console.warn stays queued', 50);
	assert.equal(posted.length, 0);
	reporting.__setConsoleSampleGateForTests(() => 0);
	console.warn('sampled warn');
	await waitFor(() => posted.length === 1, 'console-warn POST');
	assert.equal(posted[0].kind, 'console-warn');
});

test('pending shell bridge rows drain through reportClientError', async () => {
	const posted = [];
	globalThis.fetch = async (input) => {
		posted.push(await input.clone().json());
		return new Response(JSON.stringify({ event_id: 'e5', stored: true }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};
	globalThis.window.__OPENDJ_PENDING_SHELL_ERRORS__ = [
		{
			kind: 'webview-navigation',
			message: 'navigation failed: http://127.0.0.1:1/',
			context: { source: 'shell-webview', phase: 'failed' }
		}
	];
	reporting.installClientErrorReporting();
	await waitFor(() => posted.length === 1, 'pending shell drain');
	assert.equal(posted[0].kind, 'webview-navigation');
	assert.equal(globalThis.window.__OPENDJ_PENDING_SHELL_ERRORS__.length, 0);
});
