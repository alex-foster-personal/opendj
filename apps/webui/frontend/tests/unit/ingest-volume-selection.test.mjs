// requirement: LIBMX-13
// [if] two removable volumes are listed [then] the USB source list shows one row per volume, in order, [else stop].
// [if] the user clicks the second volume row [then] the click targets that volume, not the first one or an untargeted panel, [else stop].
// [if] the selected volume requires a prompt [then] the panel opens on that volume's first-seen prompt, [else stop].
// [if] no removable volumes are listed [then] the list says so and offers no row to click, [else stop].
/**
 * Issue #13407 ((private path) section 3, FLOW-01):
 * a USB row click must target the selected volume. Pinned on the real
 * UsbSourceListView (client mount, synthetic DOM) and the real tracker's
 * openUsbPanel, independently from any import rendering.
 *
 * Fixtures are synthetic: no real stick name or mount path appears here.
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { installQualityBadgeDom } from './quality-badge-client-harness.mjs';
import { bundleUsbSourceListViewClient } from './mount-svelte-client.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let client;
let tracker;
let teardownDom;

before(async () => {
	// The tracker loads before the synthetic DOM: its prefs read localStorage
	// only when a window exists, and the client-mount DOM has none.
	tracker = await loadTypeScriptModule('src/lib/rb/usb-tracker.svelte.ts');
	teardownDom = installQualityBadgeDom();
	client = await bundleUsbSourceListViewClient();
});

after(() => {
	teardownDom?.();
});

/** A present, readable removable volume as the tracker keeps it. */
function volume(id, name, extra = {}) {
	return {
		id,
		name,
		mount_path: `/Volumes/${name}`,
		kind: 'rekordbox',
		role: 'usb_stick',
		is_music: true,
		present: true,
		simulated: false,
		access: 'ok',
		first_seen: 1,
		last_seen: 2,
		...extra
	};
}

const FIRST = volume('vol:fixture-a', 'FIXTURE A');
const SECOND = volume('vol:fixture-b', 'FIXTURE B');

/** Every element under root carrying the test id, in document order. */
function allByTestId(root, testId, hits = []) {
	if (root.getAttribute?.('data-testid') === testId) hits.push(root);
	for (const child of root.childNodes ?? []) allByTestId(child, testId, hits);
	return hits;
}

/** Dispatch a bubbling synthetic click on node. */
function click(node) {
	node.dispatchEvent({
		type: 'click',
		bubbles: true,
		cancelBubble: false,
		defaultPrevented: false,
		preventDefault() {
			this.defaultPrevented = true;
		},
		stopPropagation() {
			this.cancelBubble = true;
		}
	});
}

/** Mount the real list view over volumes, recording every row callback. */
function mountList(volumes) {
	const { UsbSourceListView, mount, unmount, flushSync } = client;
	const target = document.createElement('div');
	document.body.appendChild(target);
	const calls = { toggle: [], details: [], import: [] };
	const instance = mount(UsbSourceListView, {
		target,
		props: {
			volumes,
			openSticks: {},
			canImport: () => true,
			importTitle: 'Import tracks from this stick',
			ontoggle: (vol) => calls.toggle.push(vol.id),
			ondetails: (id) => calls.details.push(id),
			onimport: (vol) => calls.import.push(vol.id),
			onrowcontextmenu: () => {},
			onrowkeydown: () => {},
			stickTree: () => {}
		}
	});
	flushSync();
	return {
		target,
		calls,
		flushSync,
		done: () => {
			unmount(instance);
			target.remove();
		}
	};
}

test('[if] two removable volumes are listed [then] one row per volume, in list order', () => {
	// Two listed sticks must render as two separately clickable rows.
	const list = mountList([FIRST, SECOND]);
	try {
		const rows = allByTestId(list.target, 'usb-source-row');
		assert.equal(rows.length, 2);
		assert.match(rows[0].textContent, /FIXTURE A/);
		assert.match(rows[1].textContent, /FIXTURE B/);
		assert.doesNotMatch(list.target.textContent, /No USB sticks/);
	} finally {
		list.done();
	}
});

test('[if] the user clicks the second volume row [then] the click carries that volume only', () => {
	// Row click and its details button both name the second stick, never the first.
	const list = mountList([FIRST, SECOND]);
	try {
		const rows = allByTestId(list.target, 'usb-source-row');
		click(rows[1]);
		list.flushSync();
		assert.deepEqual(list.calls.toggle, [SECOND.id]);

		const details = allByTestId(list.target, 'usb-source-details');
		assert.equal(details.length, 2);
		click(details[1]);
		list.flushSync();
		assert.deepEqual(list.calls.details, [SECOND.id]);
		// stopPropagation: the details click is not also a row toggle.
		assert.deepEqual(list.calls.toggle, [SECOND.id]);
		assert.deepEqual(list.calls.import, []);
	} finally {
		list.done();
	}
});

test('[if] the selected volume requires a prompt [then] the panel opens on that volume', () => {
	// openUsbPanel(id) targets the clicked stick's first-seen prompt, not another stick's.
	tracker.usbTracker.volumes = [
		volume(FIRST.id, FIRST.name, { needs_prompt: true }),
		volume(SECOND.id, SECOND.name, { needs_prompt: true })
	];
	tracker.usbTracker.panelOpen = false;
	tracker.usbTracker.promptId = null;
	tracker.openUsbPanel(SECOND.id);
	assert.equal(tracker.usbTracker.panelOpen, true);
	assert.equal(tracker.usbTracker.promptId, SECOND.id);

	// A selected stick with no pending prompt opens the panel without
	// borrowing the other stick's prompt.
	tracker.usbTracker.volumes = [
		volume(FIRST.id, FIRST.name, { needs_prompt: true }),
		volume(SECOND.id, SECOND.name, { needs_prompt: false })
	];
	tracker.usbTracker.panelOpen = false;
	tracker.usbTracker.promptId = null;
	tracker.openUsbPanel(SECOND.id);
	assert.equal(tracker.usbTracker.panelOpen, true);
	assert.equal(tracker.usbTracker.promptId, null);
});

test('[if] no removable volumes are listed [then] an empty state and no clickable row', () => {
	// With nothing plugged in there is no row to click, and nothing is called.
	const list = mountList([]);
	try {
		assert.equal(allByTestId(list.target, 'usb-source-row').length, 0);
		assert.equal(allByTestId(list.target, 'usb-source-details').length, 0);
		assert.match(list.target.textContent, /No USB sticks/);
		assert.deepEqual(list.calls, { toggle: [], details: [], import: [] });
	} finally {
		list.done();
	}
});
