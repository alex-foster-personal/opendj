/**
 * Wire-shape regression for visitor telemetry after its conversion onto the
 * generated OpenAPI client. Fire-and-forget must never throw into the app.
 */
import assert from 'node:assert/strict';
import { after, afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://telemetry.example.test';

let telemetry;
let originalFetch;

function defineGlobal(name, value) {
	Object.defineProperty(globalThis, name, {
		value,
		configurable: true,
		writable: true,
		enumerable: true
	});
}

function installBrowserGlobals() {
	defineGlobal('window', {
		location: {
			origin: 'https://app.example.test',
			pathname: '/performance',
			href: 'https://app.example.test/performance'
		},
		innerWidth: 1280,
		innerHeight: 720,
		isSecureContext: true,
		__musicDjToolsVisitorTelemetryInstalled: false
	});
	defineGlobal('document', { referrer: 'https://ref.example.test/home?q=1' });
	defineGlobal('navigator', { userAgent: 'telemetry-test-agent', language: 'en-GB' });
	defineGlobal('crypto', {
		randomUUID: () => '11111111-2222-3333-4444-555555555555'
	});
}

before(async () => {
	originalFetch = globalThis.fetch;
	installBrowserGlobals();
	telemetry = await loadTypeScriptModule('src/lib/client-telemetry.ts', { viteApiBase: API_BASE });
});

afterEach(() => {
	installBrowserGlobals();
});

after(() => {
	globalThis.fetch = originalFetch;
});

test('installVisitorTelemetry posts a page-view event once', async () => {
	let request;
	let body;
	let resolvePosted;
	const posted = new Promise((resolve) => {
		resolvePosted = resolve;
	});
	globalThis.fetch = async (input) => {
		request = input;
		body = await input.clone().json();
		resolvePosted();
		return new Response(JSON.stringify({ event_id: 'e1', stored: true }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};

	telemetry.installVisitorTelemetry();
	telemetry.installVisitorTelemetry();
	await posted;

	assert.equal(request.url, `${API_BASE}/api/v1/client-events`);
	assert.equal(request.method, 'POST');
	assert.equal(body.kind, 'page-view');
	assert.equal(body.url, 'https://app.example.test/performance');
	assert.equal(body.path, '/performance');
	if (typeof request.keepalive === 'boolean') {
		assert.equal(request.keepalive, true);
	}
});

test('markLoginSubmit then completeLibraryUsable posts one perf-span with injected delay', async () => {
	let body;
	let resolvePosted;
	const posted = new Promise((resolve) => {
		resolvePosted = resolve;
	});
	globalThis.fetch = async (input) => {
		body = await input.clone().json();
		resolvePosted();
		return new Response(JSON.stringify({ event_id: 'e2', stored: true }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};
	const storage = new Map();
	defineGlobal('sessionStorage', {
		getItem: (key) => storage.get(key) ?? null,
		setItem: (key, value) => {
			storage.set(key, value);
		},
		removeItem: (key) => {
			storage.delete(key);
		}
	});
	defineGlobal('performance', { timeOrigin: 1000 });

	telemetry.markLoginSubmit(1000);
	telemetry.markLoginNavigate(1100);
	telemetry.completeLibraryUsable({ source: 'all-tracks', now: 1300 });
	await posted;

	assert.equal(body.kind, 'perf-span');
	assert.equal(body.name, 'login-submit-to-library-usable');
	assert.ok(body.duration_ms >= 200);
});

test('completeLibraryUsable excludes Google consent from duration_ms', async () => {
	let body;
	let resolvePosted;
	const posted = new Promise((resolve) => {
		resolvePosted = resolve;
	});
	globalThis.fetch = async (input) => {
		body = await input.clone().json();
		resolvePosted();
		return new Response(JSON.stringify({ event_id: 'e2b', stored: true }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};
	const storage = new Map();
	defineGlobal('sessionStorage', {
		getItem: (key) => storage.get(key) ?? null,
		setItem: (key, value) => {
			storage.set(key, value);
		},
		removeItem: (key) => {
			storage.delete(key);
		}
	});
	defineGlobal('performance', { timeOrigin: 5000 });

	telemetry.markLoginSubmit(1000);
	telemetry.markLoginNavigate(1100);
	telemetry.completeLibraryUsable({ source: 'all-tracks', now: 5300 });
	await posted;

	assert.equal(body.duration_ms, 400);
	assert.equal(body.stages.pre_navigate_ms + body.stages.post_navigate_ms, 400);
	assert.notEqual(body.duration_ms, body.stages.full_wall_ms);
	assert.equal(body.stages.full_wall_ms, 4300);
});

test('completeLibraryUsable without a pending submit is a no-op', async () => {
	let posts = 0;
	globalThis.fetch = async () => {
		posts += 1;
		return new Response(JSON.stringify({ event_id: 'e3', stored: true }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};
	defineGlobal('sessionStorage', {
		getItem: () => null,
		setItem: () => {},
		removeItem: () => {}
	});

	telemetry.completeLibraryUsable({ source: 'playlist', now: 2000 });
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(posts, 0);
});

test('a rejecting fetch is swallowed and does not throw', async () => {
	let sawReject = false;
	let resolvePosted;
	const posted = new Promise((resolve) => {
		resolvePosted = resolve;
	});
	globalThis.fetch = async () => {
		sawReject = true;
		resolvePosted();
		throw new TypeError('network down');
	};

	assert.doesNotThrow(() => telemetry.installVisitorTelemetry());
	await posted;
	await new Promise((resolve) => setImmediate(resolve));
	assert.equal(sawReject, true);
});
