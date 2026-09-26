// requirement: PERFMODE-15
// [if] a Trackify load fails on the REAL engine path [then] exactly one toast is
//   shown, the Trackify skip toast [⛔️ if the engine's "Deck 1 could not load the
//   track" toast shows as well (issue #4036), or if no toast shows at all]
// [if] that toast is muted on the engine [then] the server still receives the
//   deck-load report with its stage context [⛔️ if muting the toast drops the report]
// [if] an ordinary (non-Trackify) load fails [then] the engine toast still shows
//   and reports [⛔️ if the fix muted the engine toast for every caller]
//
// #4038 muted only the dispatcher's toast and its test replaced engine.load, so
// it never ran the engine's own toast. Nothing here replaces engine.load: the
// real engine fetches, fails on a 404, runs its real catch block, and the real
// dispatcher and Trackify controller handle the rejection.
import assert from 'node:assert/strict';
import { afterEach, before, beforeEach, describe, it } from 'node:test';
import { fileURLToPath } from 'node:url';

import { build } from 'esbuild';

import { importBundledSource } from './import-bundled-source.mjs';
import { viteUrlSuffixPlugin } from './vite-url-suffix-plugin.mjs';

const FRONTEND_ROOT = fileURLToPath(new URL('../..', import.meta.url));
const LIB_ROOT = fileURLToPath(new URL('../../src/lib', import.meta.url));
const ENTRY = fileURLToPath(new URL('./fixtures/trackify-autoplay-entry.ts', import.meta.url));
const MISSING_ID = 'trackify-4036-missing-stable-id';

const settle = () => new Promise((resolve) => setImmediate(resolve));

/** @type {string} */
let bundleText;
/** @type {Record<string, any>} */
let entry;
/** Every client-error report body that reached the wire. */
let reports = [];
/** Every URL the engine fetched, so a test can prove the real load ran. */
let fetched = [];
let uninstallIpc = null;
let originalFetch;

function defineGlobal(name, value) {
	Object.defineProperty(globalThis, name, { value, configurable: true, writable: true, enumerable: true });
}

function installBrowserGlobals() {
	const store = new Map();
	const localStorage = {
		getItem: (key) => (store.has(key) ? store.get(key) : null),
		setItem: (key, value) => store.set(key, String(value)),
		removeItem: (key) => store.delete(key)
	};
	defineGlobal('localStorage', localStorage);
	defineGlobal('window', {
		localStorage,
		location: { href: 'https://app.example.test/music-player' },
		isSecureContext: true,
		addEventListener: () => {},
		removeEventListener: () => {}
	});
	defineGlobal('navigator', { userAgent: 'trackify-single-toast-test' });
	defineGlobal('crypto', { randomUUID: () => `id-${Math.random()}` });
}

const notFound = () =>
	new Response(JSON.stringify({ detail: { code: 'TRACK_NOT_FOUND', message: 'no such track' } }), {
		status: 404,
		headers: { 'content-type': 'application/json' }
	});

async function fakeServer(input) {
	const url = typeof input === 'string' ? input : input.url;
	fetched.push(url);
	if (url.includes('/api/v1/client-errors')) {
		reports.push(await input.clone().json());
		return new Response(JSON.stringify({ event_id: 'e', stored: true }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	}
	return notFound();
}

async function waitForReports(count) {
	for (let attempt = 0; attempt < 200 && reports.length < count; attempt += 1) {
		await new Promise((resolve) => setTimeout(resolve, 5));
	}
}

function pushedSince(before) {
	return entry.toasts.slice(before).map((toast) => [toast.kind, toast.message]);
}

function deckLoadReports() {
	return reports.filter((body) => body.context?.source === 'deck-load');
}

describe('a failed Trackify load shows one toast on the real engine path (#4036)', { concurrency: false }, () => {
	before(async () => {
		const result = await build({
			entryPoints: [ENTRY],
			absWorkingDir: FRONTEND_ROOT,
			alias: { $lib: LIB_ROOT },
			bundle: true,
			define: {
				$state: 'globalThis.__musicDjToolsTestState',
				'import.meta.env.DEV': 'true',
				'import.meta.env.VITE_API_BASE': '"https://app.example.test"'
			},
			format: 'esm',
			logLevel: 'silent',
			platform: 'node',
			plugins: [viteUrlSuffixPlugin],
			target: 'node20',
			write: false
		});
		bundleText = result.outputFiles[0].text;
		globalThis.__musicDjToolsTestState = (value) => value;
		globalThis.__musicDjToolsTestState.snapshot = (value) =>
			value === undefined ? undefined : JSON.parse(JSON.stringify(value));
		originalFetch = globalThis.fetch;
	});

	beforeEach(async () => {
		reports = [];
		fetched = [];
		installBrowserGlobals();
		globalThis.fetch = fakeServer;
		entry = await importBundledSource(bundleText, 'trackify-autoplay-entry');
		uninstallIpc = entry.installPerformanceBrowserIpc();
		entry.e2ePrimeTrackifyFeed([]);
	});

	afterEach(() => {
		if (uninstallIpc !== null) uninstallIpc();
		uninstallIpc = null;
		globalThis.fetch = originalFetch;
		delete globalThis.window;
	});

	it('[if] a Trackify load fails [then] exactly one toast shows, and it is the skip toast', async () => {
		const before = entry.toasts.length;
		await entry.e2eForceTrackifyLoad(MISSING_ID);
		await settle();
		assert.ok(
			fetched.some((url) => url.includes(MISSING_ID)),
			'the real engine load must have run; a stubbed load proves nothing about its toast'
		);
		const pushed = pushedSince(before);
		assert.equal(
			pushed.length,
			1,
			`if a failed Trackify load raises ${pushed.length} toasts then issue #4036 is back: ${JSON.stringify(pushed)}`
		);
		assert.match(pushed[0][1], /^Trackify: skipped track/, 'the one toast must be the Trackify skip toast');
		assert.equal(entry.readTrackifyAutoplayState().last_skip_reason?.startsWith(`skipped ${MISSING_ID}`), true);
	});

	it('[if] the engine toast is muted for Trackify [then] the server still gets the deck-load report with its stages', async () => {
		await entry.e2eForceTrackifyLoad(MISSING_ID);
		await waitForReports(1);
		const deckLoad = deckLoadReports();
		assert.equal(
			deckLoad.length,
			1,
			`if the muted engine toast took its server report with it then the load stage context is lost: ${JSON.stringify(reports)}`
		);
		assert.equal(deckLoad[0].context.deck, 1);
		assert.equal(
			Number.isFinite(deckLoad[0].context.stage_failedAt),
			true,
			'the report must carry WHEN the load died, or it is back to "it failed"'
		);
		assert.match(deckLoad[0].message, /TRACK_NOT_FOUND|no such track/);
	});

	it('control: an ordinary (non-Trackify) failed load still shows the engine toast and reports', async () => {
		const before = entry.toasts.length;
		await assert.rejects(
			entry.dispatchPerformanceCommand({ type: 'load', deck: 1, stable_id: MISSING_ID })
		);
		await waitForReports(1);
		const messages = pushedSince(before).map(([, message]) => message);
		assert.ok(
			messages.some((message) => message.startsWith('Deck 1 load failed - ')),
			`if the engine toast is muted for every caller then a Gig load fails silently: ${JSON.stringify(messages)}`
		);
		assert.equal(deckLoadReports().length, 1, 'the engine report must still reach the server once');
	});
});
