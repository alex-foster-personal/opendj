/**
 * Issue #4219: the waveform band palette choice is a UI pref that survives
 * reload and drives the html[data-wave-palette] attribute theme.css keys the
 * legacy override blocks on.
 *
 * Regression lines:
 * - if a first-run or pre-#4219 blob does not default to 'rekordbox' then
 *   the CDJ-parity default never ships
 * - if a stored 'legacy' does not survive reload, or does not set
 *   data-wave-palette, then the alternative resets itself every boot
 * - if a junk stored value silently resets rather than throwing then a
 *   corrupt blob is indistinguishable from first run
 * - if the setting leaves ALLOWED_SETTING_KEYS or the catalog then the
 *   Settings control (and the settings assistant's apply path) is gone
 */
import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const STORAGE_KEY = 'mdt.rb.ui-prefs.v1';
const API_BASE = 'https://wave-palette-pref.example.test';

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

test('first run defaults to rekordbox 3Band and sets no legacy attribute', async () => {
	const { root } = _fakeEnv();
	const { uiPrefs } = await _loadPrefs();
	assert.equal(uiPrefs.wave_palette, 'rekordbox');
	assert.equal(root.dataset.wavePalette, undefined);
	assert.equal(root.dataset.theme, 'dark', 'control: the theme attribute IS applied by the same boot path');
});

test('a pre-#4219 blob without the key loads the rekordbox default', async () => {
	_fakeEnv(JSON.stringify({ hide_broken_links: false, theme: 'dark' }));
	assert.equal((await _loadPrefs()).uiPrefs.wave_palette, 'rekordbox');
});

test("a stored 'legacy' survives reload and sets data-wave-palette on <html>", async () => {
	const { root } = _fakeEnv(JSON.stringify({ hide_broken_links: false, wave_palette: 'legacy' }));
	const { uiPrefs } = await _loadPrefs();
	assert.equal(uiPrefs.wave_palette, 'legacy');
	assert.equal(root.dataset.wavePalette, 'legacy');
});

test('setWavePalette persists to localStorage and toggles the attribute both ways', async () => {
	const { store, root } = _fakeEnv();
	const prefs = await _loadPrefs();
	prefs.setWavePalette('legacy');
	assert.equal(JSON.parse(store.get(STORAGE_KEY)).wave_palette, 'legacy');
	assert.equal(root.dataset.wavePalette, 'legacy');
	prefs.setWavePalette('rekordbox');
	assert.equal(JSON.parse(store.get(STORAGE_KEY)).wave_palette, 'rekordbox');
	assert.equal(root.dataset.wavePalette, undefined);
	assert.throws(() => prefs.setWavePalette('rainbow'), /rekordbox\|legacy/);
});

test('a junk stored wave_palette throws rather than silently resetting', async () => {
	_fakeEnv(JSON.stringify({ hide_broken_links: false, wave_palette: 'rainbow' }));
	await assert.rejects(() => _loadPrefs(), /wave_palette must be rekordbox\|legacy/);
});

test('the setting is an implemented enum in the catalog and is applyable', async () => {
	_fakeEnv();
	const catalog = await loadTypeScriptModule('src/lib/settings/catalog.ts', {
		viteApiBase: API_BASE
	});
	const def = catalog.SETTINGS_CATALOG.find((d) => d.id === 'wave_palette');
	assert.ok(def, 'wave_palette must be in the settings catalog');
	assert.equal(def.implemented, true);
	assert.deepEqual(
		def.control.options.map((o) => o.value),
		['rekordbox', 'legacy']
	);
	const apply = await loadTypeScriptModule('src/lib/settings/apply.ts', { viteApiBase: API_BASE });
	assert.ok(apply.ALLOWED_SETTING_KEYS.includes('wave_palette'));
	apply.applySettingChange('wave_palette', 'legacy');
	assert.equal(apply.readSettingValue('wave_palette'), 'legacy');
	assert.throws(() => apply.applySettingChange('wave_palette', 'rainbow'), /rekordbox\|legacy/);
});
