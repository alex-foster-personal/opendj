/**
 * Issue #1714: browser error classes that never reached reportClientError.
 */
import assert from 'node:assert/strict';
import { after, afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://browser-capture.example.test';
const QUEUE_KEY = 'music-dj-tools:client-errors:v1';

let reporting;
let perfLog;
let originalFetch;
let store;
let listeners;
let addEventListenerImpl;
/** lifecycle-abort's own unload listeners, registered once when the reporter module loads. */
let unloadSignalListeners;

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
		},
		debug: (...args) => {
			globalThis.__consoleDebugCalls = (globalThis.__consoleDebugCalls ?? 0) + 1;
			globalThis.__lastConsoleDebug = args;
		}
	});
}

function fire(type, event, capture = false) {
	const key = `${type}:${capture ? 'capture' : 'bubble'}`;
	for (const handler of listeners.get(key) ?? []) {
		handler(event);
	}
}

const ARTWORK_404_MARK = 'artwork-404';

function artworkNotFoundResponse() {
	return new Response(
		JSON.stringify({ detail: { code: 'ARTWORK_NOT_FOUND', message: 'no cover' } }),
		{ status: 404, headers: { 'content-type': 'application/json' } }
	);
}

/** GET confirm for marked cover imgs; POST still records client-errors bodies. */
function fetchWithArtwork404Confirm(posted, eventId = 'e-artwork') {
	return async (input, init) => {
		const req = typeof input === 'string' ? new Request(input, init) : input;
		if (req.method === 'GET' && req.url.includes('/artwork')) {
			return artworkNotFoundResponse();
		}
		posted.push(await req.clone().json());
		return new Response(JSON.stringify({ event_id: eventId, stored: true }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};
}

function markedArtworkImg(src) {
	return {
		tagName: 'IMG',
		src,
		getAttribute: (name) =>
			name === 'data-expected-resource-error' ? ARTWORK_404_MARK : null
	};
}

function readQueueLength() {
	const raw = store.getItem(QUEUE_KEY);
	if (raw === null) return 0;
	const parsed = JSON.parse(raw);
	return Array.isArray(parsed) ? parsed.length : 0;
}

before(async () => {
	originalFetch = globalThis.fetch;
	installBrowserGlobals();
	reporting = await loadTypeScriptModule('src/lib/client-error-reporting.ts', {
		viteApiBase: API_BASE
	});
	// Kept here because afterEach swaps in a fresh listener map, and the module
	// registers these once, at load.
	unloadSignalListeners = {
		beforeunload: listeners.get('beforeunload:capture') ?? [],
		pagehide: listeners.get('pagehide:capture') ?? []
	};
	perfLog = await loadTypeScriptModule('src/lib/rb/perf-event-log.ts');
	reporting.__setConsoleSampleGateForTests(() => 0);
});

afterEach(() => {
	globalThis.fetch = originalFetch;
	installBrowserGlobals();
	reporting.__resetClientErrorReportingForTests();
	reporting.__setConsoleSampleGateForTests(() => 0);
	globalThis.__consoleErrorCalls = 0;
	globalThis.__consoleWarnCalls = 0;
	globalThis.__consoleDebugCalls = 0;
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

const ISSUE_5612_ARTWORK_URL =
	'http://127.0.0.1:5273/api/v1/tracks/1bff50d66244e81df4c11ba81e375cec3d394e8d/artwork?size=orig&online=true';
// A DIFFERENT track's artwork for the unmarked control. The reporter dedupes on
// kind + source + message for 10 s, and the message embeds the URL, so a control
// sharing the marked URL would be suppressed as a duplicate whenever the marked
// event got through, and the test could not tell the two apart (PR #5614 review).
const UNMARKED_ARTWORK_URL =
	'http://127.0.0.1:5273/api/v1/tracks/0000000000000000000000000000000000005612/artwork?size=orig&online=true';

test('marked deck artwork img 404 does not POST resource-error (issue #5612)', async () => {
	const posted = [];
	globalThis.fetch = fetchWithArtwork404Confirm(posted, 'e5612');
	reporting.installClientErrorReporting();
	const markedTarget = markedArtworkImg(ISSUE_5612_ARTWORK_URL);
	fire('error', { target: markedTarget, message: 'Failed to load resource' }, true);
	const unmarkedTarget = {
		tagName: 'IMG',
		src: UNMARKED_ARTWORK_URL
	};
	fire('error', { target: unmarkedTarget, message: 'Failed to load resource' }, true);
	await waitFor(
		() => posted.some((row) => row.context?.resource === UNMARKED_ARTWORK_URL),
		'unmarked img resource-error POST'
	);
	// Positive control: the unmarked img, fired AFTER the marked one, is reported.
	const [row] = posted.filter((r) => r.context?.resource === UNMARKED_ARTWORK_URL);
	assert.equal(row.kind, 'resource-error');
	assert.equal(row.message, `Failed to load img: ${UNMARKED_ARTWORK_URL}`);
	assert.deepEqual(
		posted.map((r) => r.context?.resource),
		[UNMARKED_ARTWORK_URL],
		'the marked deck artwork must not POST; only the unmarked control may'
	);
});

const ISSUE_5613_ARTWORK_URLS = [
	'http://127.0.0.1:5273/api/v1/tracks/1bff50d66244e81df4c11ba81e375cec3d394e8d/artwork?size=s&online=true',
	'http://127.0.0.1:5273/api/v1/tracks/0000000000000000000000000000000000005613a/artwork?size=m',
	'http://127.0.0.1:5273/api/v1/tracks/0000000000000000000000000000000000005613b/artwork?size=s'
];

test(
	'marked Mixer, TrackifyPlayer, and TrackTable artwork 404s do not POST resource-error (issue #5613)',
	async () => {
		// Markup pins live in artwork-cover.test.mjs (LIBM-185 site list); here we
		// only prove capture-phase reporting skips marked artwork imgs.

		const posted = [];
		globalThis.fetch = fetchWithArtwork404Confirm(posted, 'e5613');
		reporting.installClientErrorReporting();

		for (const url of ISSUE_5613_ARTWORK_URLS) {
			const markedTarget = markedArtworkImg(url);
			fire('error', { target: markedTarget, message: 'Failed to load resource' }, true);
		}

		const nonArtworkUrl = 'https://app.example.test/skin/missing-icon.png';
		const unmarkedTarget = {
			tagName: 'IMG',
			src: nonArtworkUrl
		};
		fire('error', { target: unmarkedTarget, message: 'Failed to load resource' }, true);

		await waitFor(
			() => posted.some((row) => row.context?.resource === nonArtworkUrl),
			'unmarked non-artwork img resource-error POST'
		);

		for (const url of ISSUE_5613_ARTWORK_URLS) {
			assert.equal(
				posted.some((row) => row.context?.resource === url),
				false,
				`marked artwork must not POST: ${url}`
			);
		}

		const [row] = posted.filter((r) => r.context?.resource === nonArtworkUrl);
		assert.equal(row.kind, 'resource-error');
		assert.equal(row.message, `Failed to load img: ${nonArtworkUrl}`);
		assert.deepEqual(
			posted.map((r) => r.context?.resource),
			[nonArtworkUrl],
			'only the unmarked non-artwork control may POST'
		);
	}
);

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

test('isPerfEventConsoleLine matches recordPerfEvent console prefixes', () => {
	assert.equal(
		reporting.isPerfEventConsoleLine('[perf-event] mirror-stall: gap 2500ms (probe)'),
		true
	);
	assert.equal(reporting.isPerfEventConsoleLine('genuine operator warn'), false);
});

test('[perf-event] console.warn rows are not sampled into reportClientError', async () => {
	// No fetch stub: assert on the durable queue reportClientError writes synchronously.
	// A failed POST leaves the row queued, which is enough to prove sampling ran.
	reporting.__setConsoleSampleGateForTests(() => 0);
	reporting.installClientErrorReporting();
	perfLog.recordPerfEvent('mirror-stall', 'gap 2500ms (probe)', null, 'warn');
	await waitFor(() => readQueueLength() === 0, 'perf-event warn stays off client-errors queue', 50);
	assert.equal(readQueueLength(), 0);
	console.warn('genuine operator warn');
	await waitFor(() => readQueueLength() === 1, 'ordinary console.warn enqueues client-error');
	const queue = JSON.parse(store.getItem(QUEUE_KEY));
	assert.equal(queue[0].kind, 'console-warn');
	assert.match(queue[0].message, /genuine operator warn/);
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

// A page that is unloading kills its own in-flight fetches and module imports,
// and every caller's catch then logs or toasts. Seen as the first test of the
// webkit artifact gate (setup-entry-points.spec.ts:221), red on about half of
// main's runs with a different caller named each time.

/** The browser's unload signal, through the listener lifecycle-abort registered at module load. */
function signalUnload(type) {
	const handlers = unloadSignalListeners[type];
	assert.ok(handlers.length > 0, `the reporter must listen for ${type} in the capture phase`);
	for (const handler of handlers) handler({ type });
}

function offline() {
	// A failed POST leaves the row in the durable queue, which is what these tests read.
	globalThis.fetch = async () => {
		throw new TypeError('offline');
	};
}

for (const signal of ['beforeunload', 'pagehide']) {
	test(`console.error after ${signal} prints at debug level and files nothing`, () => {
		offline();
		reporting.installClientErrorReporting();
		signalUnload(signal);
		console.error(
			'[entitlements] load failed; nothing is treated as refused',
			new TypeError('Load failed')
		);
		assert.equal(globalThis.__consoleErrorCalls ?? 0, 0, 'no error-level line for a killed request');
		assert.equal(globalThis.__consoleDebugCalls, 1, 'the line stays readable at debug level');
		assert.match(String(globalThis.__lastConsoleDebug[0]), /entitlements/);
		assert.equal(readQueueLength(), 0, 'a killed request is not filed as a client error');
	});
}

test('console.warn while the page is unloading prints at debug level and files nothing', () => {
	offline();
	reporting.installClientErrorReporting();
	signalUnload('beforeunload');
	console.warn('[analysis-source] poll failed; retrying on the next tick');
	assert.equal(globalThis.__consoleWarnCalls ?? 0, 0);
	assert.equal(globalThis.__consoleDebugCalls, 1);
	assert.equal(readQueueLength(), 0);
});

test('console.error on a page that stays open is still an error and is still filed', () => {
	offline();
	reporting.installClientErrorReporting();
	console.error('[entitlements] load failed; nothing is treated as refused');
	assert.equal(globalThis.__consoleErrorCalls, 1, 'a live page keeps its error-level line');
	assert.equal(globalThis.__consoleDebugCalls ?? 0, 0);
	assert.equal(readQueueLength(), 1, 'a live page still files the console error');
});

test('an unhandled rejection and a failed resource load file nothing while unloading, and file on a live page', () => {
	offline();
	reporting.installClientErrorReporting();
	const killedImport = { reason: new TypeError('Importing a module script failed.') };
	const killedPreload = {
		target: { tagName: 'LINK', href: 'https://app.example.test/_app/immutable/chunks/a.js' }
	};
	signalUnload('beforeunload');
	fire('unhandledrejection', killedImport);
	fire('error', killedPreload, true);
	assert.equal(readQueueLength(), 0, 'what the unload killed is not filed');

	reporting.__resetClientErrorReportingForTests();
	installBrowserGlobals();
	offline();
	reporting.installClientErrorReporting();
	fire('unhandledrejection', killedImport);
	fire('error', killedPreload, true);
	const kinds = JSON.parse(store.getItem(QUEUE_KEY)).map((row) => row.kind);
	assert.deepEqual(kinds, ['unhandled-rejection', 'resource-error'], 'a live page files both');
});

test('the unload silence ends with its window, so a navigation that never left does not mute the page', () => {
	offline();
	reporting.installClientErrorReporting();
	const realNow = Date.now;
	const beforeSignal = realNow();
	signalUnload('beforeunload');
	const afterSignal = realNow();
	try {
		Date.now = () => beforeSignal + reporting.UNLOAD_NOISE_WINDOW_MS - 1;
		console.error('inside the window');
		assert.equal(globalThis.__consoleErrorCalls ?? 0, 0, 'inside the window the unload is still the cause');
		Date.now = () => afterSignal + reporting.UNLOAD_NOISE_WINDOW_MS + 1;
		console.error('the page is still here');
		assert.equal(globalThis.__consoleErrorCalls, 1, 'past the window an error is an error again');
		assert.equal(readQueueLength(), 1, 'and is filed again');
	} finally {
		Date.now = realNow;
	}
});

test('a deliberate reportClientError while unloading is still filed (the quit gate reports on purpose)', () => {
	offline();
	reporting.installClientErrorReporting();
	signalUnload('beforeunload');
	reporting.reportClientError(
		new Error('quit flush failed'),
		{ source: 'quit-gate', route: 'lifecycle/quit' },
		'unhandled-rejection'
	);
	assert.equal(readQueueLength(), 1, 'only ambient capture goes quiet, never an explicit report');
});
