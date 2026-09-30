/**
 * LIBUX-28 / PRRT_kwDOSEvNd86mix-e: compatible-filter ranges survive a fresh
 * browser profile because GET /api/v1/ui-prefs hydrates them over the
 * localStorage defaults, and a range change PUTs the object to disk.
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://compatible-filter-hydrate.example.test';

let prefs;
let originalFetch;

function jsonResponse(body) {
	return new Response(JSON.stringify(body), {
		status: 200,
		headers: { 'content-type': 'application/json' }
	});
}

before(async () => {
	prefs = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE });
	originalFetch = globalThis.fetch;
});

after(() => {
	globalThis.fetch = originalFetch;
});

const DEFAULTS = {
	camelot_steps: 1,
	bpm_window_bpm: 20,
	bpm_enabled: true,
	allow_half_double: true,
	bpm_direction: 'both'
};

test('a fresh profile hydrates compatible_filter from GET /api/v1/ui-prefs', async () => {
	prefs.uiPrefs.compatible_filter = { ...DEFAULTS };
	const onDisk = {
		camelot_steps: 2,
		bpm_window_bpm: 10,
		bpm_enabled: false,
		allow_half_double: false,
		bpm_direction: 'below'
	};
	globalThis.fetch = async () => jsonResponse({ theme: 'dark', compatible_filter: onDisk });

	await prefs.hydrateConfirmPrefsFromDisk();

	assert.deepEqual(prefs.uiPrefs.compatible_filter, onDisk);
});

test('a partial disk object fills the remaining fields from the defaults', async () => {
	prefs.uiPrefs.compatible_filter = { ...DEFAULTS, bpm_direction: 'above' };
	globalThis.fetch = async () =>
		jsonResponse({ compatible_filter: { camelot_steps: 0 }, library_watcher_folders: ['/Users/dev/w'] });

	await prefs.hydrateConfirmPrefsFromDisk();

	assert.deepEqual(prefs.uiPrefs.compatible_filter, { ...DEFAULTS, camelot_steps: 0 });
	assert.deepEqual(prefs.uiPrefs.library_watcher_folders, ['/Users/dev/w']);
});

test('an invalid disk object is skipped and the rest of the hydrate still applies', async () => {
	const before = { ...DEFAULTS, camelot_steps: 2 };
	prefs.uiPrefs.compatible_filter = { ...before };
	prefs.uiPrefs.library_watcher_folders = [];
	const originalError = console.error;
	const logged = [];
	console.error = (...args) => logged.push(args);
	try {
		globalThis.fetch = async () =>
			jsonResponse({
				compatible_filter: { camelot_steps: 7 },
				library_watcher_folders: ['/Users/dev/after']
			});
		await prefs.hydrateConfirmPrefsFromDisk();
	} finally {
		console.error = originalError;
	}

	assert.deepEqual(prefs.uiPrefs.compatible_filter, before);
	assert.deepEqual(prefs.uiPrefs.library_watcher_folders, ['/Users/dev/after']);
	assert.equal(logged.length, 1);
});

test('a GET without compatible_filter leaves the local ranges alone', async () => {
	const local = { ...DEFAULTS, bpm_window_bpm: 10 };
	prefs.uiPrefs.compatible_filter = { ...local };
	globalThis.fetch = async () => jsonResponse({ theme: 'dark' });

	await prefs.hydrateConfirmPrefsFromDisk();

	assert.deepEqual(prefs.uiPrefs.compatible_filter, local);
});

test('patchCompatibleFilter PUTs the whole compatible_filter object', async () => {
	prefs.uiPrefs.compatible_filter = { ...DEFAULTS };
	let body;
	let release;
	const gate = new Promise((resolve) => {
		release = resolve;
	});
	globalThis.fetch = async (request) => {
		body = await request.clone().json();
		release();
		return jsonResponse({});
	};

	prefs.patchCompatibleFilter({ bpm_direction: 'same' });
	await gate;

	assert.deepEqual(body, { compatible_filter: { ...DEFAULTS, bpm_direction: 'same' } });
});
