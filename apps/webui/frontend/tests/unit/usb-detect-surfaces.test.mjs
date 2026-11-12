import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { afterEach, describe, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

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

function _row(id, name) {
	return {
		id,
		name,
		first_seen: 1,
		last_seen: 1,
		present: true,
		needs_prompt: true,
		is_music: true,
		role: 'usb_stick'
	};
}

afterEach(() => {
	delete globalThis.window;
});

describe('usb detect findability (USB-328, #11165)', () => {
	test('[if] new usb_stick appears [then] toast pulse and openUsbPanel path wire without modal', async () => {
		const panel = readFileSync('src/lib/components/rb/BrowserPanel.svelte', 'utf8');
		const nav = readFileSync('src/lib/components/rb/browser/LibraryNav.svelte', 'utf8');
		const tracker = readFileSync('src/lib/rb/usb-tracker.svelte.ts', 'utf8');
		assert.match(
			panel,
			/data-testid="bottom-bar-usb-detect"[\s\S]{0,240}onclick=\{\(\) => openUsbPanel\(\)\}/
		);
		assert.match(
			nav,
			/data-testid="library-usb-panel-entry"[\s\S]{0,200}onclick=\{\(\) => openUsbPanel\(\)\}/
		);
		assert.match(tracker, /pushToast\([\s\S]{0,120}`USB detected: \$\{row\.name\}`/);
		assert.match(tracker, /'usb-detect'[\s\S]{0,200}handler: \(\) => openUsbPanel\(row\.id\)/);
		assert.doesNotMatch(tracker, /needs_prompt[\s\S]{0,80}usbTracker\.panelOpen = true/);
	});

	test('[if] usb_toast_enabled is false [then] no detect toast is shown', async () => {
		_fakeWindow(JSON.stringify({ hide_broken_links: false, usb_toast_enabled: false }));
		const prefs = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts');
		const tracker = await loadTypeScriptModule('src/lib/rb/usb-tracker.svelte.ts');
		const stores = await loadTypeScriptModule('src/lib/stores.svelte.ts');
		const before = stores.toasts.length;
		assert.equal(prefs.uiPrefs.usb_toast_enabled, false);
		tracker.clearUsbDetectPulses();
		tracker.applyUsbDetectSignals(_row('vol:a', 'Stick A'));
		assert.equal(stores.toasts.length, before);
		assert.equal(tracker.usbDetectPulseActive(), true);
	});

	test('[if] default toast duration is used [then] it lasts 60s and can be dismissed', async () => {
		_fakeWindow();
		const prefs = await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts');
		assert.equal(prefs.uiPrefs.usb_toast_ms, 60_000);
		const stores = await loadTypeScriptModule('src/lib/stores.svelte.ts');
		const timers = [];
		const realSetTimeout = globalThis.setTimeout;
		globalThis.setTimeout = (fn, ms) => {
			timers.push({ name: fn.name, ms });
			return 0;
		};
		try {
			stores.pushToast(
				'usb detected',
				'info',
				prefs.uiPrefs.usb_toast_ms,
				undefined,
				{},
				'usb-detect',
				{
					label: 'Open USB panel',
					handler: () => {}
				}
			);
			assert.equal(timers.filter((t) => t.name === '').map((t) => t.ms)[0], 60_000);
		} finally {
			globalThis.setTimeout = realSetTimeout;
		}
	});

	test('[if] two sticks are present [then] pulse and panel list both, not one flag', async () => {
		const tracker = await loadTypeScriptModule('src/lib/rb/usb-tracker.svelte.ts');
		tracker.clearUsbDetectPulses();
		tracker.markUsbDetectPulse('vol:one');
		tracker.markUsbDetectPulse('vol:two');
		assert.deepEqual(tracker.usbDetectPulseIds().sort(), ['vol:one', 'vol:two']);
		tracker.usbTracker.volumes = [_row('vol:one', 'One'), _row('vol:two', 'Two')];
		const active = tracker.presentNonForgotten();
		assert.equal(active.length, 2);
	});

	test('[if] usb_auto_open_panel is false [then] detect does not open the panel', async () => {
		_fakeWindow(
			JSON.stringify({
				hide_broken_links: false,
				usb_toast_enabled: false,
				usb_auto_open_panel: false
			})
		);
		await loadTypeScriptModule('src/lib/rb/prefs.svelte.ts');
		const tracker = await loadTypeScriptModule('src/lib/rb/usb-tracker.svelte.ts');
		tracker.usbTracker.panelOpen = false;
		tracker.usbTracker.promptId = null;
		tracker.clearUsbDetectPulses();
		tracker.applyUsbDetectSignals(_row('vol:quiet', 'Quiet'));
		assert.equal(tracker.usbTracker.panelOpen, false);
		assert.equal(tracker.usbTracker.promptId, null);
	});
});
