// requirement: TOPVIEW-01
// [if] the daemon refuses ui-prefs.json, or serves a malformed top-bar view [then] hydration rejects and the local view is kept, [else stop].
// [if] the feature-icon click mode cannot be read or saved in browser storage [then] that failure is reported, [else stop].
//
// Sol P1s on PR #6228 (review of 005e9b460).

import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://topbar-sol-p1.example.test';

let prefs;
let originalFetch;

before(async () => {
	prefs = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE });
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

async function captureErrors(fn) {
	const originalError = console.error;
	const logged = [];
	console.error = (...args) => logged.push(args.map(String).join(' '));
	try {
		await fn();
	} finally {
		console.error = originalError;
	}
	return logged;
}

function response(status, body) {
	return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

// Since main's PR #7483 the hydrator has no blanket catch: a refused or
// malformed prefs file rejects to the boot path, which shows it. These keep
// the top-bar half of that contract: the local view survives either way.
test('a 422 on GET /ui-prefs rejects and the local top-bar view is kept', async () => {
	prefs.uiPrefs.perf_view = 'list';
	globalThis.fetch = async () =>
		response(422, { detail: { code: 'VALIDATION', message: 'perf_view must be one of the views' } });
	await assert.rejects(() => prefs.hydrateConfirmPrefsFromDisk(), /perf_view must be one of the views/);
	assert.equal(prefs.uiPrefs.perf_view, 'list');
});

test('a malformed top-bar view in a served prefs file fails hydration and keeps the local view', async () => {
	prefs.uiPrefs.perf_view = 'list';
	globalThis.fetch = async () => response(200, { perf_view: 'grid', key_notation: 'camelot_musical' });
	await assert.rejects(() => prefs.hydrateConfirmPrefsFromDisk(), /Top-bar view prefs on disk were refused/);
	assert.equal(prefs.uiPrefs.perf_view, 'list');
});

test('feature-icon click mode reports storage that throws on read and on write', async () => {
	const originalStorage = globalThis.localStorage;
	globalThis.localStorage = {
		getItem() {
			throw new Error('SecurityError: storage disabled');
		},
		setItem() {
			throw new Error('QuotaExceededError');
		}
	};
	let cluster;
	try {
		const readLogged = await captureErrors(async () => {
			cluster = await loadTypeScriptModule('src/lib/rb/feature-icon-cluster.svelte.ts');
		});
		assert.equal(cluster.featureIconSingleClick(), false);
		assert.equal(readLogged.length, 1);
		assert.match(readLogged[0], /could not be read from this browser's storage/);

		const writeLogged = await captureErrors(() => cluster.setFeatureIconSingleClick(true));
		assert.equal(cluster.featureIconSingleClick(), true, 'the choice still applies for this page');
		assert.equal(writeLogged.length, 1);
		assert.match(writeLogged[0], /could not be saved in this browser's storage; it applies until you reload/);
	} finally {
		if (originalStorage === undefined) delete globalThis.localStorage;
		else globalThis.localStorage = originalStorage;
	}
});

// Sol P1 on 81615f231: only "0" and "1" are click-mode values; anything else
// stored is reported rather than silently read as the one-button strip.
test('feature-icon click mode reads 0 and 1 and reports any other stored value', async () => {
	const originalStorage = globalThis.localStorage;
	const withStored = async (value) => {
		globalThis.localStorage = {
			getItem: () => value,
			setItem() {}
		};
		let cluster;
		const logged = await captureErrors(async () => {
			cluster = await loadTypeScriptModule('src/lib/rb/feature-icon-cluster.svelte.ts');
		});
		return { on: cluster.featureIconSingleClick(), logged };
	};
	try {
		assert.deepEqual(await withStored(null), { on: false, logged: [] });
		assert.deepEqual(await withStored('0'), { on: false, logged: [] });
		assert.deepEqual(await withStored('1'), { on: true, logged: [] });
		const bad = await withStored('yes');
		assert.equal(bad.on, false);
		assert.equal(bad.logged.length, 1, `expected one report, got ${JSON.stringify(bad.logged)}`);
		assert.match(bad.logged[0], /is "yes", not 0 or 1/);
	} finally {
		if (originalStorage === undefined) delete globalThis.localStorage;
		else globalThis.localStorage = originalStorage;
	}
});

// Sol P1 on 8f0330108: a browser with no storage API reports on read and on
// write; server-side evaluation (no window) stays silent.
test('feature-icon click mode reports a browser with no storage, and is silent with no window', async () => {
	const originalStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
	const originalWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
	const restore = () => {
		if (originalStorage) Object.defineProperty(globalThis, 'localStorage', originalStorage);
		else delete globalThis.localStorage;
		if (originalWindow) Object.defineProperty(globalThis, 'window', originalWindow);
		else delete globalThis.window;
	};
	try {
		Object.defineProperty(globalThis, 'localStorage', { value: undefined, configurable: true, writable: true });

		delete globalThis.window;
		let cluster;
		const serverLogged = await captureErrors(async () => {
			cluster = await loadTypeScriptModule('src/lib/rb/feature-icon-cluster.svelte.ts');
			cluster.setFeatureIconSingleClick(true);
		});
		assert.deepEqual(serverLogged, [], 'server-side evaluation has nothing to report');

		globalThis.window = {};
		const readLogged = await captureErrors(async () => {
			cluster = await loadTypeScriptModule('src/lib/rb/feature-icon-cluster.svelte.ts');
		});
		assert.equal(cluster.featureIconSingleClick(), false);
		assert.equal(readLogged.length, 1);
		assert.match(readLogged[0], /cannot be read: this browser has no storage/);

		const writeLogged = await captureErrors(() => cluster.setFeatureIconSingleClick(true));
		assert.equal(cluster.featureIconSingleClick(), true, 'the choice still applies for this page');
		assert.equal(writeLogged.length, 1);
		assert.match(writeLogged[0], /cannot be saved: this browser has no storage/);
	} finally {
		restore();
	}
});
