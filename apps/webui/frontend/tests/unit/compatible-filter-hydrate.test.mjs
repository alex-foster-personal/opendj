/**
 * LIBUX-32 / PRRT_kwDOSEvNd86mix-e: compatible-filter ranges survive a fresh
 * browser profile because GET /api/v1/ui-prefs hydrates them over the
 * localStorage defaults, and a range change PUTs the object to disk.
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://compatible-filter-hydrate.example.test';

let prefs;
let originalFetch;

/** A GET /api/v1/ui-prefs body always carries a valid key_notation (the
 * contract hydration enforces since PR #7483); success stubs model that. */
function jsonResponse(body) {
	return new Response(JSON.stringify({ key_notation: 'camelot', library_hidden_columns: [], library_key_filter: [], ...body }), {
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
	rules: ['same_key', 'adjacent', 'relative'],
	bpm_window_bpm: 20,
	bpm_enabled: true,
	allow_half_double: true,
	bpm_direction: 'both',
	mode: 'transition',
	key_shift_max: 1,
	mashup_relative: false
};

test('a fresh profile hydrates compatible_filter from GET /api/v1/ui-prefs', async () => {
	prefs.uiPrefs.compatible_filter = { ...DEFAULTS };
	const onDisk = {
		rules: ['diagonal', 'jaws'],
		bpm_window_bpm: 10,
		bpm_enabled: false,
		allow_half_double: false,
		bpm_direction: 'below',
		mode: 'mashup',
		key_shift_max: 2,
		mashup_relative: true
	};
	globalThis.fetch = async () => jsonResponse({ theme: 'dark', compatible_filter: onDisk });

	await prefs.hydrateConfirmPrefsFromDisk();

	assert.deepEqual(prefs.uiPrefs.compatible_filter, onDisk);
});

test('a partial disk object fills the remaining fields from the defaults', async () => {
	prefs.uiPrefs.compatible_filter = { ...DEFAULTS, bpm_direction: 'above' };
	globalThis.fetch = async () =>
		jsonResponse({ compatible_filter: { rules: ['relative'] }, library_watcher_folders: ['/Users/dj/w'] });

	await prefs.hydrateConfirmPrefsFromDisk();

	assert.deepEqual(prefs.uiPrefs.compatible_filter, { ...DEFAULTS, rules: ['relative'] });
	assert.deepEqual(prefs.uiPrefs.library_watcher_folders, ['/Users/dj/w']);
});

test('an invalid disk object is skipped and the rest of the hydrate still applies', async () => {
	const before = { ...DEFAULTS, rules: ['energy_boost'] };
	prefs.uiPrefs.compatible_filter = { ...before };
	prefs.uiPrefs.library_watcher_folders = [];
	const originalError = console.error;
	const logged = [];
	console.error = (...args) => logged.push(args);
	try {
		globalThis.fetch = async () =>
			jsonResponse({
				compatible_filter: { rules: ['tritone'] },
				library_watcher_folders: ['/Users/dj/after']
			});
		await prefs.hydrateConfirmPrefsFromDisk();
	} finally {
		console.error = originalError;
	}

	assert.deepEqual(prefs.uiPrefs.compatible_filter, before);
	assert.deepEqual(prefs.uiPrefs.library_watcher_folders, ['/Users/dj/after']);
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

test('a disk object from before rule checkboxes reads camelot_steps as its nearest rules', async () => {
	for (const [steps, rules] of [
		[0, ['same_key', 'relative']],
		[1, ['same_key', 'adjacent', 'relative']],
		[2, ['same_key', 'adjacent', 'relative', 'energy_boost', 'energy_drop']]
	]) {
		prefs.uiPrefs.compatible_filter = { ...DEFAULTS };
		globalThis.fetch = async () => jsonResponse({ compatible_filter: { camelot_steps: steps } });
		await prefs.hydrateConfirmPrefsFromDisk();
		assert.deepEqual(prefs.uiPrefs.compatible_filter.rules, rules, `camelot_steps ${steps}`);
	}
});

test('a legacy camelot_steps other than 0, 1 or 2 is corrupt and throws, never read as the defaults', async () => {
	const mod = await loadTypeScriptModule('src/lib/rb/compatible-filter-prefs.ts');
	for (const bad of [3, -1, '1', true, null, []]) {
		assert.throws(
			() => mod.validateCompatibleFilterPrefs({ camelot_steps: bad }, 'k'),
			/compatible_filter\.camelot_steps invalid/,
			`camelot_steps ${JSON.stringify(bad)}`
		);
	}
	assert.deepEqual(mod.validateCompatibleFilterPrefs({ camelot_steps: 0 }, 'k').rules, ['same_key', 'relative']);
});
