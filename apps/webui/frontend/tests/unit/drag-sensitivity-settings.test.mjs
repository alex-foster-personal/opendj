// R6 (Wed 7 Oct 2026): a Drag sensitivity section in Config with live test controls.
// [if] the base slider (or the agent bridge) changes [then] every drag uses it at once, no reload, [else stop].
// [if] the value is persisted [then] it survives a reload through uiPrefs, [else stop].
// [if] a stored value is out of range [then] load throws rather than silently resetting, [else stop].
// [if] the Config row loses its test knobs and faders [then] this fails, [else stop].
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { afterEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const STORAGE_KEY = 'mdt.rb.ui-prefs.v1';
const SRC = new URL('../../src/', import.meta.url);

afterEach(() => {
	delete globalThis.window;
	delete globalThis.document;
});

function installBrowser(store = new Map()) {
	globalThis.window = {
		localStorage: {
			getItem: (k) => (store.has(k) ? store.get(k) : null),
			setItem: (k, v) => void store.set(k, String(v)),
			removeItem: (k) => void store.delete(k)
		}
	};
	globalThis.document = { documentElement: { dataset: {}, style: {} } };
	return store;
}

test('R6: a fresh profile drags at 0.7x with a 150 px falloff', async () => {
	installBrowser();
	const prefs = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts');
	assert.deepEqual(prefs.uiPrefs.drag_sensitivity, { base: 0.7, falloff_px: 150 });
});

test('R6: the setter applies live, persists, and a reload reads it back', async () => {
	const store = installBrowser();
	const prefs = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts');
	prefs.setDragSensitivity({ base: 1.2 });
	const saved = JSON.parse(store.get(STORAGE_KEY));
	assert.deepEqual(saved.drag_sensitivity, { base: 1.2, falloff_px: 150 });
	installBrowser(store);
	const again = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts');
	assert.equal(again.uiPrefs.drag_sensitivity.base, 1.2);
});

test('R6: the bridge and the Config setting both drive the shared drag state', async () => {
	installBrowser();
	const prefs = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts');
	const bridge = globalThis.window.__mdtDragSensitivity;
	assert.ok(bridge, 'window.__mdtDragSensitivity is not installed');
	bridge.set({ falloff_px: 300 });
	assert.deepEqual(bridge.get(), { base: 0.7, falloff_px: 300 });
	assert.throws(() => bridge.set({ base: 9 }), RangeError);
	bridge.reset();
	assert.deepEqual(prefs.uiPrefs.drag_sensitivity, { base: 0.7, falloff_px: 150 });
});

test('R6: the bridge rejects malformed payloads instead of keeping the old value', async () => {
	installBrowser();
	const prefs = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts');
	const bridge = globalThis.window.__mdtDragSensitivity;
	assert.throws(() => bridge.set({ falloffPx: 300 }), /only base and falloff_px may be set, got falloffPx/);
	assert.throws(() => bridge.set(null), TypeError);
	assert.throws(() => bridge.set({}), /got no fields/);
	assert.throws(() => bridge.set([]), TypeError);
	assert.deepEqual(prefs.uiPrefs.drag_sensitivity, { base: 0.7, falloff_px: 150 });
	// Control: a well-formed partial still merges with the stored value.
	bridge.set({ base: 1.1 });
	assert.deepEqual(prefs.uiPrefs.drag_sensitivity, { base: 1.1, falloff_px: 150 });
});

test('R6: a stored out-of-range value throws on load instead of resetting quietly', async () => {
	const store = installBrowser();
	const prefs = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts');
	prefs.setDragSensitivity({ base: 1 });
	const blob = JSON.parse(store.get(STORAGE_KEY));
	blob.drag_sensitivity = { base: 0, falloff_px: 150 };
	store.set(STORAGE_KEY, JSON.stringify(blob));
	installBrowser(store);
	await assert.rejects(loadTypeScriptModule('src/lib/rb/prefs.svelte.ts'), /drag_sensitivity\.base/);
});

test('R6: the gain reads the live state, so a change needs no reload', async () => {
	const m = await loadTypeScriptModule('src/lib/rb/drag-sensitivity.svelte.ts');
	const t1 = m.createDragTracker(0, 0);
	const before = m.trackDragMove(t1, 0, -2).dy;
	m.applyDragSensitivity({ base: 1.4, falloff_px: 150 });
	const t2 = m.createDragTracker(0, 0);
	const after = m.trackDragMove(t2, 0, -2).dy;
	assert.ok(Math.abs(after / before - 2) < 0.01, `doubling the base gave ${after / before}x`);
});

test('R6 wiring: Config has the rows, the reset, and two test knobs plus two test faders', () => {
	const catalog = readFileSync(new URL('lib/settings/catalog.ts', SRC), 'utf8');
	assert.match(catalog, /id: 'drag_sensitivity\.base'[\s\S]*tester: 'drag'/);
	assert.match(catalog, /id: 'drag_sensitivity\.falloff_px'/);
	const apply = readFileSync(new URL('lib/settings/apply.ts', SRC), 'utf8');
	assert.match(apply, /'drag_sensitivity\.base',\s*'drag_sensitivity\.falloff_px'/);
	const overlay = readFileSync(new URL('lib/components/settings/SettingsOverlay.svelte', SRC), 'utf8');
	assert.match(
		overlay,
		/def\.tester === 'drag'[\s\S]*<DragSensitivityTester\s+onresetsensitivity=\{\(\) =>\s*setDragSensitivity\(\{ base: DRAG_DEFAULTS\.base, falloff_px: DRAG_DEFAULTS\.falloffPx \}\)\}/
	);
	assert.match(overlay, /\{#await import\('\.\/DragSensitivityTester\.svelte'\)\}[\s\S]*\{:catch err\}/);
	assert.doesNotMatch(
		overlay,
		/import DragSensitivityTester from/,
		'the root layout mounts this overlay, so a static import puts the mixer drag code in the library initial load'
	);
	const tester = readFileSync(new URL('lib/components/settings/DragSensitivityTester.svelte', SRC), 'utf8');
	assert.equal((tester.match(/<Knob /g) ?? []).length, 2);
	assert.equal((tester.match(/<DragTestFader /g) ?? []).length, 2);
	assert.match(tester, /Reset drag sensitivity/);
	assert.match(tester, /onresetsensitivity\(\);/, 'the reset button must put the stored sensitivity back');
	assert.doesNotMatch(tester, /\$lib\/rb\/prefs\.svelte/, 'the tester takes its reset from the overlay (prefs fan-in ratchet)');
	assert.doesNotMatch(tester, /runPerformanceCommandFromUi|dispatchPerformanceCommand/, 'test controls must be wired to nothing');
	const fader = readFileSync(new URL('lib/components/settings/DragTestFader.svelte', SRC), 'utf8');
	assert.match(fader, /trackDragMove\(tracker, e\.clientX, e\.clientY\)/);
	assert.match(fader, /stepSelectionDrag\(group, delta, e\.altKey\)/);
});
