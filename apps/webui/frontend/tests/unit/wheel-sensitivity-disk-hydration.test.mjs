/**
 * Disk-backed wheel sensitivity wins over stale legacy localStorage on hydrate
 * (issue #13207, spec 7f98c552 "Wheel sensitivity").
 *
 * Contract pinned here, through the real GET /api/v1/ui-prefs hydrator:
 * - if disk and the legacy `mdt.rb.wheel-sensitivity.v1` blob disagree, disk wins
 *   and the legacy blob is rewritten so a reload cannot resurrect the stale value;
 * - if disk carries no wheel_sensitivity, the legacy local value survives untouched;
 * - if disk carries a partial (or partly invalid) wheel object, only the valid keys
 *   it carries win and every other key keeps its legacy local value.
 */
import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://wheel-disk-hydration.example.test';
const LEGACY_STORAGE_KEY = 'mdt.rb.wheel-sensitivity.v1';

const originalFetch = globalThis.fetch;
const originalWindow = globalThis.window;

afterEach(() => {
	globalThis.fetch = originalFetch;
	if (originalWindow === undefined) delete globalThis.window;
	else globalThis.window = originalWindow;
});

/** Install a fake window whose localStorage starts with the given legacy wheel blob. */
function installLegacyWheel(legacy) {
	const store = new Map();
	if (legacy !== null) store.set(LEGACY_STORAGE_KEY, JSON.stringify(legacy));
	globalThis.window = {
		localStorage: {
			getItem: (key) => (store.has(key) ? store.get(key) : null),
			setItem: (key, value) => store.set(key, String(value)),
			removeItem: (key) => store.delete(key)
		}
	};
	return store;
}

/** Stub GET /api/v1/ui-prefs with a contract-valid body plus the given fields. */
function stubDisk(fields) {
	globalThis.fetch = async () =>
		new Response(
			JSON.stringify({
				key_notation: 'camelot',
				library_hidden_columns: [],
				library_key_filter: [],
				...fields
			}),
			{ status: 200, headers: { 'content-type': 'application/json' } }
		);
}

/** Load a fresh prefs module (and its wheel store) after legacy storage is seeded. */
async function loadPrefs() {
	return loadTypeScriptModule('src/lib/rb/prefs.svelte.ts', { viteApiBase: API_BASE });
}

/** Live wheel factors via the agent bridge the bundled wheel store installs. */
function liveWheel() {
	return globalThis.window.__mdtWheelSensitivity.get();
}

test('disk wheel_sensitivity wins over a disagreeing legacy local blob', async () => {
	// Disk and legacy local disagree on both keys: disk must win deterministically.
	const store = installLegacyWheel({ mouse: 0.5, trackpad: 0.7 });
	const prefs = await loadPrefs();
	assert.deepEqual(liveWheel(), { mouse: 0.5, trackpad: 0.7 }, 'legacy blob loads first');

	stubDisk({ wheel_sensitivity: { mouse: 2, trackpad: 0.25 } });
	await prefs.hydrateConfirmPrefsFromDisk();

	assert.deepEqual(liveWheel(), { mouse: 2, trackpad: 0.25 });
	assert.deepEqual(JSON.parse(store.get(LEGACY_STORAGE_KEY)), { mouse: 2, trackpad: 0.25 });
});

test('disk precedence is stable across repeated hydrations', async () => {
	// A second identical GET must not flip back to the stale local value.
	installLegacyWheel({ mouse: 3, trackpad: 3 });
	const prefs = await loadPrefs();
	stubDisk({ wheel_sensitivity: { mouse: 1.5, trackpad: 0.4 } });

	await prefs.hydrateConfirmPrefsFromDisk();
	await prefs.hydrateConfirmPrefsFromDisk();

	assert.deepEqual(liveWheel(), { mouse: 1.5, trackpad: 0.4 });
});

test('legacy local wheel value survives when disk has no wheel_sensitivity', async () => {
	// No disk value means no silent loss of the legacy local setting.
	const store = installLegacyWheel({ mouse: 0.5, trackpad: 0.7 });
	const prefs = await loadPrefs();
	const rawBefore = store.get(LEGACY_STORAGE_KEY);

	stubDisk({});
	await prefs.hydrateConfirmPrefsFromDisk();

	assert.deepEqual(liveWheel(), { mouse: 0.5, trackpad: 0.7 });
	assert.equal(store.get(LEGACY_STORAGE_KEY), rawBefore, 'legacy blob is not rewritten');
});

test('a partial disk wheel object overrides only the keys it carries', async () => {
	// Disk carries mouse only: mouse follows disk, trackpad keeps its legacy value.
	const store = installLegacyWheel({ mouse: 0.5, trackpad: 0.7 });
	const prefs = await loadPrefs();

	stubDisk({ wheel_sensitivity: { mouse: 2 } });
	await prefs.hydrateConfirmPrefsFromDisk();

	assert.deepEqual(liveWheel(), { mouse: 2, trackpad: 0.7 });
	assert.deepEqual(JSON.parse(store.get(LEGACY_STORAGE_KEY)), { mouse: 2, trackpad: 0.7 });
});

test('invalid keys in a partial disk wheel object are skipped, valid ones still win', async () => {
	// A bad key must not throw, reset, or block the valid key beside it.
	installLegacyWheel({ mouse: 0.5, trackpad: 0.7 });
	const prefs = await loadPrefs();

	stubDisk({ wheel_sensitivity: { mouse: 'fast', trackpad: 1.25 } });
	await prefs.hydrateConfirmPrefsFromDisk();
	assert.deepEqual(liveWheel(), { mouse: 0.5, trackpad: 1.25 });

	stubDisk({ wheel_sensitivity: { mouse: 99, trackpad: -1 } });
	await prefs.hydrateConfirmPrefsFromDisk();
	assert.deepEqual(
		liveWheel(),
		{ mouse: 0.5, trackpad: 1.25 },
		'out-of-range disk values are ignored'
	);
});

test('a partial disk wheel object with no legacy blob fills the rest from defaults', async () => {
	// First run on this browser: disk mouse applies, trackpad stays at the declared default.
	installLegacyWheel(null);
	const prefs = await loadPrefs();
	const defaults = globalThis.window.__mdtWheelSensitivity.defaults();

	stubDisk({ wheel_sensitivity: { mouse: 2 } });
	await prefs.hydrateConfirmPrefsFromDisk();

	assert.deepEqual(liveWheel(), { mouse: 2, trackpad: defaults.trackpad });
});
