import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// USB tracker prefs + the toast dismiss delay they feed. Regression lines:
// - if usb_toast_enabled is absent or defaults false then _onNewDetect's
//   `if (uiPrefs.usb_toast_enabled)` is dead and the detect toast never
//   fires for anyone -- broken (this is the bug these prefs fix)
// - if usb_toast_ms defaults to something other than the 5000 pushToast
//   used to hardcode then existing toasts silently change duration -- broken
// - if usb_auto_open_panel defaults true then every stick that needs
//   nothing yanks the panel open -- broken
// - if a malformed persisted blob is silently reset instead of throwing
//   then the fail-fast prefs policy is gone -- broken
// - if pushToast accepts a zero/negative/NaN delay then a toast either
//   never dismisses or dismisses instantly -- broken
// - if pushToast's third argument is ignored then usb_toast_ms does
//   nothing and the pref is decorative -- broken

/** Install a localStorage-backed fake window so prefs._load reads `raw`. */
function _fakeWindow(raw) {
	const store = new Map();
	if (raw !== undefined) store.set('mdt.rb.ui-prefs.v1', raw);
	globalThis.window = {
		localStorage: {
			getItem: (k) => (store.has(k) ? store.get(k) : null),
			setItem: (k, v) => store.set(k, v)
		}
	};
	return store;
}

afterEach(() => {
	delete globalThis.window;
});

async function _loadPrefs() {
	return loadTypeScriptModule('src/lib/rb/prefs.svelte.ts');
}

test('first run defaults carry all three USB prefs', async () => {
	_fakeWindow(); // no stored blob at all = genuine first run
	const prefs = await _loadPrefs();

	assert.equal(prefs.uiPrefs.usb_toast_enabled, true);
	assert.equal(prefs.uiPrefs.usb_toast_ms, 5000);
	assert.equal(prefs.uiPrefs.usb_auto_open_panel, false);
});

test('a blob saved before these prefs existed takes the defaults', async () => {
	// The realistic upgrade path: everyone already has a v1 blob on disk
	// that predates these three keys. It must not throw, and must not
	// leave the toast pref undefined, which is what made it dead.
	_fakeWindow(JSON.stringify({ hide_broken_links: false, theme: 'dark' }));
	const prefs = await _loadPrefs();

	assert.equal(prefs.uiPrefs.usb_toast_enabled, true);
	assert.equal(prefs.uiPrefs.usb_toast_ms, 5000);
	assert.equal(prefs.uiPrefs.usb_auto_open_panel, false);
});

test('stored USB prefs survive the round trip', async () => {
	_fakeWindow(
		JSON.stringify({
			hide_broken_links: false,
			usb_toast_enabled: false,
			usb_toast_ms: 1200,
			usb_auto_open_panel: true
		})
	);
	const prefs = await _loadPrefs();

	assert.equal(prefs.uiPrefs.usb_toast_enabled, false);
	assert.equal(prefs.uiPrefs.usb_toast_ms, 1200);
	assert.equal(prefs.uiPrefs.usb_auto_open_panel, true);
});

test('a malformed USB pref throws rather than silently resetting', async () => {
	for (const bad of [
		{ usb_toast_enabled: 'yes' },
		{ usb_toast_ms: 0 },
		{ usb_toast_ms: -5 },
		{ usb_toast_ms: 'soon' },
		{ usb_auto_open_panel: 1 }
	]) {
		_fakeWindow(JSON.stringify({ hide_broken_links: false, ...bad }));
		await assert.rejects(
			() => _loadPrefs(),
			/malformed prefs blob/,
			`should reject ${JSON.stringify(bad)}`
		);
		delete globalThis.window;
	}
});

test('pushToast honours a caller-supplied dismiss delay', async () => {
	const stores = await loadTypeScriptModule('src/lib/stores.svelte.ts');
	assert.equal(stores.TOAST_DEFAULT_MS, 5000);

	// pushToast also records the toast into the perf-event ring, and that ring
	// arms its own coalesced localStorage flush on a timer. The flush is not a
	// dismiss timer, so it is filtered out by callback identity rather than
	// allowed to masquerade as a caller-supplied delay: a dismissal is an
	// anonymous arrow, the flush is the named flushPerfEventLog.
	const timers = [];
	const realSetTimeout = globalThis.setTimeout;
	globalThis.setTimeout = (fn, ms) => {
		timers.push({ name: fn.name, ms });
		return 0;
	};
	try {
		stores.pushToast('default delay', 'info');
		stores.pushToast('usb detected', 'info', 1200);
		assert.deepEqual(
			timers.filter((timer) => timer.name === '').map((timer) => timer.ms),
			[5000, 1200]
		);
		assert.ok(
			timers.some((timer) => timer.name.startsWith('flushPerfEventLog')),
			'if the toast stops reaching the perf ring then a dismissed toast leaves no ' +
				'trace behind, which is why recordPerfEvent sits in pushToast at all'
		);
	} finally {
		globalThis.setTimeout = realSetTimeout;
	}

	assert.equal(stores.toasts.length, 2);
	assert.equal(stores.toasts[0].message, 'default delay');
	assert.equal(stores.toasts[1].kind, 'info');
});

test('pushToast fails fast on a delay that would never dismiss', async () => {
	const stores = await loadTypeScriptModule('src/lib/stores.svelte.ts');

	for (const bad of [0, -1, Number.NaN, Number.POSITIVE_INFINITY]) {
		assert.throws(
			() => stores.pushToast('bad', 'info', bad),
			/dismissMs must be a positive finite number/,
			`should reject ${String(bad)}`
		);
	}
	assert.equal(stores.toasts.length, 0, 'a rejected toast must not be pushed');
});
