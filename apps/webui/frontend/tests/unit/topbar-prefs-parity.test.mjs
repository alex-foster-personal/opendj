import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const WEBUI_ROOT = join(dirname(fileURLToPath(import.meta.url)), '../../..');
const FRONTEND_ROOT = join(dirname(fileURLToPath(import.meta.url)), '../..');
const STORAGE_KEY = 'mdt.rb.ui-prefs.v1';
const API_BASE = 'https://topbar-prefs.example.test';

/** TopBar control -> [setter, pref key, value written in the test]. */
const TOPBAR_PREFS = {
	beat_sync_max: ['setBeatSyncMax', false],
	auto_play_enabled: ['setAutoPlayEnabled', false],
	auto_play_enforce_order: ['setAutoPlayEnforceOrder', true],
	auto_play_maximize_reach: ['setAutoPlayMaximizeReach', false]
};

const UI_PREFS_PY = readFileSync(join(WEBUI_ROOT, 'server/routes/ui_prefs.py'), 'utf8');
const PREFS_SVELTE = readFileSync(join(FRONTEND_ROOT, 'src/lib/rb/prefs.svelte.ts'), 'utf8');
const PREFS_HYDRATE = readFileSync(join(FRONTEND_ROOT, 'src/lib/rb/prefs-hydrate.ts'), 'utf8');
const TOPBAR = readFileSync(join(FRONTEND_ROOT, 'src/lib/components/rb/TopBar.svelte'), 'utf8');

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

test('each TopBar pref setter persists locally and PUTs its disk patch', async () => {
	for (const [key, [setter, value]] of Object.entries(TOPBAR_PREFS)) {
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

test('TopBar imports every setter and the four keys exist on the HTTP prefs route', () => {
	for (const [key, [setter]] of Object.entries(TOPBAR_PREFS)) {
		assert.match(TOPBAR, new RegExp(`\\b${setter}\\b`), `TopBar must call ${setter}`);
		assert.match(UI_PREFS_PY, new RegExp(`"${key}"`), `ui_prefs.py must serve ${key}`);
		assert.match(PREFS_HYDRATE, new RegExp(`\\b${key}\\b`), `DiskPrefsPatch must include ${key}`);
		assert.match(
			PREFS_SVELTE,
			new RegExp(`setTopbarDiskPref\\([^)]*'${key}'`),
			`${setter} must sync ${key} to disk`
		);
		assert.match(PREFS_HYDRATE, /setTopbarDiskPref/, 'disk sync helper must live in prefs-hydrate');
	}
});
