// requirement: CHROME-12, CHROME-13
// [if] the top-bar skin button is clicked [then] it walks default -> Gothic -> Light and wraps, persists through ui_skin, and its tooltip names current and next, [else stop].
// [if] the light skin is declared [then] it defines every token mono-dev and the base palette define and its text pairs meet AA, [else stop].
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const THEME = readFileSync('src/lib/rb/theme.css', 'utf8');
const TOPBAR = readFileSync('src/lib/components/rb/TopBar.svelte', 'utf8');
const STORAGE_KEY = 'mdt.rb.ui-prefs.v1';

let skin;
let cc;
before(async () => {
	skin = await loadTypeScriptModule('src/lib/rb/ui-skin.ts');
	cc = await loadTypeScriptModule('src/lib/rb/color-contrast.ts');
});

/** Every custom property (any value, not just hex) in the block whose selector is exactly `selector`. */
function customProps(selector) {
	const start = THEME.indexOf(`${selector} {`);
	assert.ok(start !== -1, `no \`${selector} {}\` block in theme.css`);
	const body = THEME.slice(start, THEME.indexOf('}', start));
	return new Set([...body.matchAll(/(--[\w-]+):/g)].map((m) => m[1]));
}

/** Fake browser globals so prefs.svelte.ts persists and applies the skin like it does in the app. */
function installBrowser(store = new Map()) {
	const localStorage = {
		getItem: (k) => (store.has(k) ? store.get(k) : null),
		setItem: (k, v) => void store.set(k, String(v)),
		removeItem: (k) => void store.delete(k)
	};
	globalThis.window = { localStorage };
	globalThis.document = { documentElement: { dataset: {}, style: {} } };
	return store;
}

test('if the skin cycle order is not default, Gothic, Light and wrapping then the button skips or strands a skin', () => {
	assert.deepEqual([...skin.UI_SKIN_CHOICES], ['default', 'mono-dev', 'light']);
	assert.equal(skin.nextUiSkin('default'), 'mono-dev');
	assert.equal(skin.nextUiSkin('mono-dev'), 'light');
	assert.equal(skin.nextUiSkin('light'), 'default');
	let s = 'default';
	const seen = [];
	for (let i = 0; i < 6; i += 1) {
		s = skin.nextUiSkin(s);
		seen.push(s);
	}
	assert.deepEqual(seen, ['mono-dev', 'light', 'default', 'mono-dev', 'light', 'default']);
	assert.throws(() => skin.nextUiSkin('neon'), /ui_skin must be default\|mono-dev\|light/);
});

test('if the tooltip does not name the current skin and the next one then the button reads as a mystery toggle', () => {
	assert.equal(skin.skinCycleTitle('default'), 'Skin: Default. Click for Gothic');
	assert.equal(skin.skinCycleTitle('mono-dev'), 'Skin: Gothic. Click for Light');
	assert.equal(skin.skinCycleTitle('light'), 'Skin: Light. Click for Default');
});

test('if light is not a parseable skin then set_skin and the ui_skin setting cannot reach it (agent parity)', () => {
	assert.equal(skin.parseUiSkin('light'), 'light');
	const doc = { documentElement: { dataset: { skin: 'mono-dev' } } };
	const prev = globalThis.document;
	globalThis.document = doc;
	try {
		skin.applyUiSkinDom('light');
		assert.equal(doc.documentElement.dataset.skin, 'light');
		skin.applyUiSkinDom('default');
		assert.equal('skin' in doc.documentElement.dataset, false);
	} finally {
		globalThis.document = prev;
	}
});

test('if cycleUiSkin does not persist ui_skin and apply data-skin then the choice is lost on reload', async () => {
	const store = installBrowser();
	const prefs = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts');
	// CHROME-14: a first run boots Gothic; the cycle order itself is unchanged.
	assert.equal(prefs.uiPrefs.ui_skin, 'mono-dev');
	assert.equal(prefs.cycleUiSkin(), 'light');
	assert.equal(JSON.parse(store.get(STORAGE_KEY)).ui_skin, 'light');
	assert.equal(globalThis.document.documentElement.dataset.skin, 'light');
	assert.equal(prefs.cycleUiSkin(), 'default');
	assert.equal(JSON.parse(store.get(STORAGE_KEY)).ui_skin, 'default');
	assert.equal('skin' in globalThis.document.documentElement.dataset, false);
	assert.equal(prefs.cycleUiSkin(), 'mono-dev');
	assert.equal(JSON.parse(store.get(STORAGE_KEY)).ui_skin, 'mono-dev');
	assert.equal(globalThis.document.documentElement.dataset.skin, 'mono-dev');
});

test('if a stored light skin is not restored on boot then persistence only works within one session', async () => {
	// Write the blob through the real setter: the loader rejects a partial blob.
	const store = installBrowser();
	const first = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts');
	first.setUiSkin('light');
	installBrowser(store);
	const prefs = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts');
	assert.equal(prefs.uiPrefs.ui_skin, 'light');
	assert.equal(globalThis.document.documentElement.dataset.skin, 'light');
	assert.equal(prefs.cycleUiSkin(), 'default');
});

test('if the top-bar button still toggles the theme then it does not cycle skins', () => {
	const at = TOPBAR.indexOf('onclick={cycleUiSkin}');
	assert.ok(at !== -1, 'skin button must call cycleUiSkin');
	const button = TOPBAR.slice(TOPBAR.lastIndexOf('<button', at), TOPBAR.indexOf('</button>', at));
	assert.match(button, /title=\{skinCycleTitle\(uiPrefs\.ui_skin\)\}/);
	assert.match(button, /aria-label=\{skinCycleTitle\(uiPrefs\.ui_skin\)\}/);
	assert.doesNotMatch(TOPBAR, /toggleTheme/);
});

test('if the light skin misses a token mono-dev or the base palette defines then a dark value leaks into light', () => {
	const light = customProps("html[data-skin='light'] .perf-root");
	const mono = customProps("html[data-skin='mono-dev'] .perf-root");
	const baseColors = Object.keys(cc.parseColorTokens(THEME, '.perf-root')).map((k) => `--${k}`);
	assert.ok(mono.size >= 30, `mono-dev parse looks short (${mono.size})`);
	assert.ok(baseColors.length >= 20, `base palette parse looks short (${baseColors.length})`);
	assert.deepEqual([...mono].filter((t) => !light.has(t)), []);
	assert.deepEqual(baseColors.filter((t) => !light.has(t)), []);
});

test('if the light skin text or indicator pairs fail contrast then readouts are illegible', () => {
	const tokens = cc.parseColorTokens(THEME, "html[data-skin='light'] .perf-root");
	assert.equal(tokens['rb-bg'], '#f7f3eb', 'light ground from the claude.ai Light Pass');
	assert.deepEqual(cc.validateScheme(tokens), []);
});
