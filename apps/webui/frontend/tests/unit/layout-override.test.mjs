// requirement: SKINLAYOUT-10199
// [if] layout_override auto does not follow the skin, or an unknown preset loads silently [then] agents cannot trust layout readback, [else stop].
// [if] set_layout_preset or Settings rows are missing [then] HTTP and CLI cannot set the override, [else stop].
import assert from 'node:assert/strict';
import { afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const STORAGE_KEY = 'mdt.rb.ui-prefs.v1';
const API_BASE = 'https://layout-override.example.test';

let L;
let skin;
let ipc;

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

before(async () => {
	L = await loadTypeScriptModule('src/lib/rb/skin-layout.ts');
	skin = await loadTypeScriptModule('src/lib/rb/ui-skin.ts');
	await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE });
	ipc = await loadTypeScriptModule('src/lib/rb/performance-ipc.svelte.ts', {
		viteApiBase: API_BASE
	});
});

afterEach(() => {
	delete globalThis.window;
	delete globalThis.document;
});

test('effective layouts follow the skin on auto and explicit presets win', () => {
	assert.equal(
		L.effectivePerformanceLayout('auto', 'green-room'),
		L.performanceLayoutFor('green-room')
	);
	assert.equal(L.effectiveLibraryLayout('auto', 'green-room'), 'three-panel');
	assert.equal(L.effectiveLibraryLayout('three-panel', 'mono-dev'), 'three-panel');
	assert.equal(L.effectiveLibraryLayout('crate-desk', 'green-room'), 'crate-desk');
	for (const s of skin.UI_SKIN_CHOICES) {
		assert.equal(L.effectivePerformanceLayout('deck-grid', s), 'deck-grid');
	}
});

test('parseLayoutOverride accepts valid blobs and throws on bad presets or keys', () => {
	assert.deepEqual(L.parseLayoutOverride({ performance: 'auto', library: 'three-panel' }), {
		performance: 'auto',
		library: 'three-panel'
	});
	assert.throws(
		() => L.parseLayoutOverride({ performance: 'auto', library: 'sidebar-left' }),
		/auto\|crate-desk\|three-panel/
	);
	assert.throws(
		() => L.parseLayoutOverride({ performance: 'auto', library: 'auto', mixer: 'auto' }),
		/performance\|library/
	);
	assert.throws(() => L.parseLayoutOverride({ performance: 'auto' }), /missing key library/);
	assert.throws(() => L.parseLayoutOverride('three-panel'), /must be an object/);
});

test('prefs load, persist and refuse malformed layout_override', async () => {
	const { store } = _fakeEnv();
	let prefs = await _loadPrefs();
	assert.deepEqual(prefs.uiPrefs.layout_override, { performance: 'auto', library: 'auto' });

	_fakeEnv(JSON.stringify({ hide_broken_links: false, theme: 'dark' }));
	assert.deepEqual((await _loadPrefs()).uiPrefs.layout_override, {
		performance: 'auto',
		library: 'auto'
	});

	_fakeEnv(
		JSON.stringify({
			hide_broken_links: false,
			layout_override: { performance: 'auto', library: 'three-panel' }
		})
	);
	assert.deepEqual((await _loadPrefs()).uiPrefs.layout_override.library, 'three-panel');

	_fakeEnv(
		JSON.stringify({
			hide_broken_links: false,
			layout_override: { library: 'sidebar-left', performance: 'auto' }
		})
	);
	await assert.rejects(() => _loadPrefs(), /mdt\.rb\.ui-prefs\.v1/);

	_fakeEnv(
		JSON.stringify({
			hide_broken_links: false,
			layout_override: 'three-panel'
		})
	);
	await assert.rejects(() => _loadPrefs(), /mdt\.rb\.ui-prefs\.v1/);

	const { store: persistStore } = _fakeEnv();
	prefs = await _loadPrefs();
	prefs.setLayoutOverride('library', 'three-panel');
	assert.equal(JSON.parse(persistStore.get(STORAGE_KEY)).layout_override.library, 'three-panel');
	const before = prefs.uiPrefs.layout_override.library;
	assert.throws(() => prefs.setLayoutOverride('library', 'nope'), /auto\|crate-desk\|three-panel/);
	assert.equal(prefs.uiPrefs.layout_override.library, before);
});

test('settings catalog rows and apply round-trip', async () => {
	_fakeEnv();
	const catalog = await loadTypeScriptModule('src/lib/settings/catalog.ts', {
		viteApiBase: API_BASE
	});
	for (const id of ['layout_override.performance', 'layout_override.library']) {
		const def = catalog.SETTINGS_CATALOG.find((d) => d.id === id);
		assert.ok(def, id);
		assert.equal(def.group, 'performance');
		assert.equal(def.implemented, true);
		const options = def.control.options.map((o) => o.value);
		const surface = id.endsWith('performance') ? 'performance' : 'library';
		assert.deepEqual(options, [...L.layoutPresetChoices(surface)]);
	}
	const search = await loadTypeScriptModule('src/lib/settings/search.ts', {
		viteApiBase: API_BASE
	});
	const { all: hits } = search.filterSettings('layout', { hideTodo: true, group: null });
	assert.ok(hits.some((h) => h.id === 'layout_override.performance'));
	assert.ok(hits.some((h) => h.id === 'layout_override.library'));
	const apply = await loadTypeScriptModule('src/lib/settings/apply.ts', { viteApiBase: API_BASE });
	assert.ok(apply.ALLOWED_SETTING_KEYS.includes('layout_override.performance'));
	assert.ok(apply.ALLOWED_SETTING_KEYS.includes('layout_override.library'));
	apply.applySettingChange('layout_override.library', 'three-panel');
	assert.equal(apply.readSettingValue('layout_override.library'), 'three-panel');
	assert.throws(
		() => apply.applySettingChange('layout_override.library', 'nope'),
		/auto\|crate-desk\|three-panel/
	);
});

function _ipcStorage() {
	const store = new Map();
	const storage = {
		getItem: (k) => (store.has(k) ? store.get(k) : null),
		setItem: (k, v) => store.set(k, v),
		removeItem: (k) => store.delete(k)
	};
	globalThis.window = { localStorage: storage };
	return storage;
}

test('set_layout_preset reaches prefs, snapshot and refuses bad pairings', async () => {
	_ipcStorage();
	const uninstall = ipc.installPerformanceBrowserIpc();
	const dispatch = (c) => globalThis.window.musicDjToolsPerformance.dispatch(c);
	const ui = () => ipc.queryPerformanceState().ui;
	try {
		await dispatch({ type: 'set_layout_preset', surface: 'performance', preset: 'deck-grid' });
		assert.equal(ui().layout.performance, 'deck-grid');
		assert.equal(ui().layout_override.performance, 'deck-grid');
		await dispatch({ type: 'set_layout_preset', surface: 'library', preset: 'three-panel' });
		assert.equal(ui().layout.library, 'three-panel');
		await dispatch({ type: 'set_layout_preset', surface: 'library', preset: 'auto' });
		assert.equal(ui().layout.library, L.libraryLayoutFor(ui().ui_skin));
		await assert.rejects(
			dispatch({ type: 'set_layout_preset', surface: 'library', preset: 'deck-grid' }),
			/library layout must be auto\|crate-desk\|three-panel/
		);
		await assert.rejects(
			dispatch({ type: 'set_layout_preset', surface: 'mixer', preset: 'auto' }),
			/performance\|library/
		);
		await assert.rejects(
			dispatch({ type: 'set_layout_preset', surface: 'library', preset: 'three-panel', extra: 1 }),
			/unexpected fields/
		);
	} finally {
		uninstall();
		delete globalThis.window;
	}
});
