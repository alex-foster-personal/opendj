import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const WEBUI_ROOT = join(dirname(fileURLToPath(import.meta.url)), '../../..');
const FRONTEND_ROOT = join(dirname(fileURLToPath(import.meta.url)), '../..');
const STORAGE_KEY = 'mdt.rb.ui-prefs.v1';
const API_BASE = 'https://library-browser-prefs.example.test';

/** Library browser control -> [setter, pref key, value written in the test]. */
const LIBRARY_BROWSER_PREFS = {
	hide_broken_links: ['setHideBrokenLinks', true],
	library_density: ['setLibraryDensity', 'cosy'],
	next_only_filter: ['setNextOnlyFilter', true],
	remixes_filter: ['setRemixesFilter', true],
	vocals_filter: ['setVocalsFilter', true],
	available_offline_filter: ['setAvailableOfflineFilter', true]
};

const UI_PREFS_PY = readFileSync(join(WEBUI_ROOT, 'server/routes/ui_prefs.py'), 'utf8');
const PREFS_SVELTE = readFileSync(join(FRONTEND_ROOT, 'src/lib/rb/prefs.svelte.ts'), 'utf8');
const PREFS_HYDRATE = readFileSync(join(FRONTEND_ROOT, 'src/lib/rb/prefs-hydrate.ts'), 'utf8');
const LIBRARY_FILTER_PREFS = readFileSync(
	join(FRONTEND_ROOT, 'src/lib/rb/library-filter-prefs.ts'),
	'utf8'
);
const WHEEL_ADJUST = readFileSync(join(FRONTEND_ROOT, 'src/lib/rb/wheel-adjust.ts'), 'utf8');
const MIDI_UI_STATE = readFileSync(
	join(FRONTEND_ROOT, 'src/lib/components/rb/midi/midi-ui-state.svelte.ts'),
	'utf8'
);

function _fakeWindow(raw) {
	const store = new Map();
	if (raw !== undefined) store.set(STORAGE_KEY, raw);
	globalThis.window = {
		localStorage: {
			getItem: (k) => (store.has(k) ? store.get(k) : null),
			setItem: (k, v) => store.set(k, v)
		}
	};
	return store;
}

function _stored(store) {
	return JSON.parse(store.get(STORAGE_KEY));
}

async function _loadPrefs() {
	return loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE });
}

function _capturePuts() {
	const bodies = [];
	const original = globalThis.fetch;
	globalThis.fetch = async (request) => {
		bodies.push(await request.clone().json());
		return new Response(JSON.stringify({ theme: 'dark' }), {
			status: 200,
			headers: { 'content-type': 'application/json' }
		});
	};
	return {
		bodies,
		restore: () => {
			globalThis.fetch = original;
		}
	};
}

afterEach(() => {
	delete globalThis.window;
});

test('each library-browser pref setter persists locally and PUTs its disk patch', async () => {
	for (const [key, [setter, value]] of Object.entries(LIBRARY_BROWSER_PREFS)) {
		const store = _fakeWindow();
		const puts = _capturePuts();
		try {
			const prefs = await _loadPrefs();
			prefs[setter](value);
			assert.equal(prefs.uiPrefs[key], value, `${setter} did not set ${key}`);
			assert.equal(_stored(store)[key], value, `${setter} did not persist ${key}`);
			await new Promise((resolve) => setTimeout(resolve, 0));
			assert.deepEqual(puts.bodies, [{ [key]: value }], `${setter} disk patch`);
		} finally {
			puts.restore();
			delete globalThis.window;
		}
	}
});

test('library-browser keys exist on the HTTP route, DiskPrefsPatch, and setter disk sync', () => {
	for (const [key, [setter]] of Object.entries(LIBRARY_BROWSER_PREFS)) {
		assert.match(UI_PREFS_PY, new RegExp(`"${key}"`), `ui_prefs.py must serve ${key}`);
		assert.match(PREFS_HYDRATE, new RegExp(`\\b${key}\\b`), `DiskPrefsPatch must include ${key}`);
		if (
			key === 'next_only_filter' ||
			key === 'remixes_filter' ||
			key === 'vocals_filter' ||
			key === 'available_offline_filter'
		) {
			assert.match(
				LIBRARY_FILTER_PREFS,
				new RegExp(`syncDiskPrefs\\(\\{ \\[key\\]: next \\}`),
				`${setter} must sync ${key} to disk`
			);
		} else {
			assert.match(
				PREFS_SVELTE,
				new RegExp(`setLibraryBrowserDiskPref\\([^)]*'${key}'`),
				`${setter} must sync ${key} to disk`
			);
		}
	}
	assert.match(PREFS_HYDRATE, /setLibraryBrowserDiskPref/, 'disk sync helper must live in prefs-hydrate');
});

test('setWheelSensitivity PUTs wheel_sensitivity and the key is on the HTTP route', async () => {
	const store = new Map();
	globalThis.window = {
		localStorage: {
			getItem: (k) => (store.has(k) ? store.get(k) : null),
			setItem: (k, v) => store.set(k, v)
		}
	};
	const puts = _capturePuts();
	try {
		const wheel = await loadTypeScriptModule('src/lib/rb/wheel-adjust.ts', { viteApiBase: API_BASE });
		wheel.setWheelSensitivity('mouse', 2);
		await new Promise((resolve) => setTimeout(resolve, 0));
		assert.deepEqual(puts.bodies, [{ wheel_sensitivity: { mouse: 2, trackpad: wheel.WHEEL_SENSITIVITY.trackpad } }]);
		assert.match(UI_PREFS_PY, /"wheel_sensitivity"/, 'ui_prefs.py must serve wheel_sensitivity');
		assert.match(WHEEL_ADJUST, /_syncWheelSensitivityToDisk/, 'wheel setter must sync to disk');
	} finally {
		puts.restore();
		delete globalThis.window;
	}
});

test('setMidiEnabledChoice PUTs midi_enabled and the key is on the HTTP route', async () => {
	const store = new Map();
	globalThis.localStorage = {
		getItem: (k) => (store.has(k) ? store.get(k) : null),
		setItem: (k, v) => store.set(k, v),
		removeItem: (k) => store.delete(k)
	};
	const puts = _capturePuts();
	try {
		const midi = await loadTypeScriptModule(
			'src/lib/components/rb/midi/midi-ui-state.svelte.ts',
			{ viteApiBase: API_BASE }
		);
		midi.setMidiEnabledChoice(true);
		await new Promise((resolve) => setTimeout(resolve, 0));
		assert.deepEqual(puts.bodies, [{ midi_enabled: true }]);
		assert.match(UI_PREFS_PY, /"midi_enabled"/, 'ui_prefs.py must serve midi_enabled');
		assert.match(MIDI_UI_STATE, /_syncMidiEnabledToDisk/, 'midi setter must sync to disk');
	} finally {
		puts.restore();
		delete globalThis.localStorage;
	}
});
