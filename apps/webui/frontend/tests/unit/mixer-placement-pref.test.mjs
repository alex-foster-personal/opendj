/**
 * Issue #9434: mixer_placement browser pref (center default, sides round-trip).
 * "Disk prefs shape" is the mdt.rb.ui-prefs.v1 localStorage JSON blob only.
 *
 * Regression lines:
 * - if first run does not default to center then shipped layout defaults are wrong
 * - if sides does not survive persist + reload then the pref is not durable
 * - if unknown values silently reset then corrupt blobs are indistinguishable from first run
 * - if the setting leaves ALLOWED_SETTING_KEYS or the catalog then Settings apply is broken
 */
import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const STORAGE_KEY = 'mdt.rb.ui-prefs.v1';
const API_BASE = 'https://mixer-placement-pref.example.test';

function _fakeEnv(raw) {
	const store = new Map();
	if (raw !== undefined) store.set(STORAGE_KEY, raw);
	globalThis.window = {
		localStorage: {
			getItem: (k) => (store.has(k) ? store.get(k) : null),
			setItem: (k, v) => store.set(k, v)
		}
	};
	const root = { dataset: {}, style: {} };
	globalThis.document = { documentElement: root };
	return { store, root };
}

async function _loadPrefs() {
	return loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE });
}

afterEach(() => {
	delete globalThis.window;
	delete globalThis.document;
});

test('first run defaults mixer_placement to center', async () => {
	_fakeEnv();
	const { uiPrefs } = await _loadPrefs();
	assert.equal(uiPrefs.mixer_placement, 'center');
});

test('a pre-#9434 blob without the key loads center', async () => {
	_fakeEnv(JSON.stringify({ hide_broken_links: false, mixer_stems: true }));
	const { uiPrefs } = await _loadPrefs();
	assert.equal(uiPrefs.mixer_placement, 'center');
});

test('setMixerPlacement sides persists to localStorage and reloads as sides', async () => {
	const { store } = _fakeEnv();
	const prefs = await _loadPrefs();
	prefs.setMixerPlacement('sides');
	assert.equal(prefs.uiPrefs.mixer_placement, 'sides');
	assert.equal(JSON.parse(store.get(STORAGE_KEY)).mixer_placement, 'sides');

	_fakeEnv(store.get(STORAGE_KEY));
	const { uiPrefs: reloaded } = await _loadPrefs();
	assert.equal(reloaded.mixer_placement, 'sides');
});

test('a junk stored mixer_placement throws with center|sides in the message', async () => {
	_fakeEnv(JSON.stringify({ hide_broken_links: false, mixer_placement: 'left' }));
	await assert.rejects(() => _loadPrefs(), /center\|sides/);
	await assert.rejects(() => _loadPrefs(), /left/);
});

test('setMixerPlacement refuses left and leaves uiPrefs unchanged', async () => {
	_fakeEnv();
	const prefs = await _loadPrefs();
	assert.throws(() => prefs.setMixerPlacement('left'), /center\|sides/);
	assert.equal(prefs.uiPrefs.mixer_placement, 'center');
});

test('the setting is in the catalog, allowlist and apply path', async () => {
	_fakeEnv();
	const catalog = await loadTypeScriptModule('src/lib/settings/catalog.ts', {
		viteApiBase: API_BASE
	});
	const def = catalog.SETTINGS_CATALOG.find((d) => d.id === 'mixer_placement');
	assert.ok(def, 'mixer_placement must be in the settings catalog');
	assert.equal(def.implemented, true);
	assert.deepEqual(
		def.control.options.map((o) => o.value),
		['center', 'sides']
	);
	const apply = await loadTypeScriptModule('src/lib/settings/apply.ts', { viteApiBase: API_BASE });
	assert.ok(apply.ALLOWED_SETTING_KEYS.includes('mixer_placement'));
	apply.applySettingChange('mixer_placement', 'sides');
	assert.equal(apply.readSettingValue('mixer_placement'), 'sides');
	assert.throws(() => apply.applySettingChange('mixer_placement', 'left'), /center\|sides/);
});
