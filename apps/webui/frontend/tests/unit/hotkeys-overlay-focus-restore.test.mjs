/**
 * Issue #12259: closing a filtered hotkeys overlay returns focus to its opener.
 *
 * The overlay dialog (HotkeysOverlayView.svelte) mounts the shared focusTrap
 * action, whose destroy() runs when the overlay store hides it. The filter
 * query lives in the overlay machine and never reaches the trap; hiding from a
 * filtered state is pinned in hotkeys-overlay.test.mjs. These tests tear the
 * trap down with focus where Escape and the close button leave it.
 *
 * [if] Escape closes a filtered hotkeys overlay [then] focus returns to the
 * connected opener
 * [if] the close button closes a filtered hotkeys overlay [then] focus returns
 * to the connected opener
 * [if] the opener no longer exists on close [then] the focused element inside
 * the overlay is blurred so no stale focused element is left
 */

import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let trapMod;
let realDocument;
let fakeDocument;

before(async () => {
	trapMod = await loadTypeScriptModule('src/lib/keyboard/focus-trap.ts');
	realDocument = globalThis.document;
});

after(() => {
	globalThis.document = realDocument;
});

/** Build a fake focusable element that records focus and blur on the fake document. */
function fakeEl(label) {
	return {
		label,
		tabIndex: 0,
		isConnected: true,
		matches: () => false,
		closest: () => null,
		checkVisibility: () => true,
		getClientRects: () => [{ width: 1 }],
		focus() {
			fakeDocument.activeElement = this;
		},
		blur() {
			if (fakeDocument.activeElement === this) fakeDocument.activeElement = fakeDocument.body;
		}
	};
}

/** Mount a trapped hotkeys dialog over a focused opener. */
async function openTrappedOverlay() {
	const body = { label: 'body', focus() {} };
	const opener = fakeEl('opener');
	fakeDocument = {
		activeElement: opener,
		body,
		addEventListener() {},
		removeEventListener() {}
	};
	globalThis.document = fakeDocument;

	const search = fakeEl('search');
	const close = fakeEl('close');
	const node = {
		label: 'dialog',
		tabIndex: -1,
		isConnected: true,
		contains: (el) => el === node || el === search || el === close,
		querySelector: (sel) => (sel === '.hk-search' ? search : null),
		querySelectorAll: () => [search, close],
		focus() {
			fakeDocument.activeElement = this;
		}
	};
	const trap = trapMod.focusTrap(node, { initialFocus: '.hk-search' });
	await Promise.resolve();
	return { trap, opener, search, close, body };
}

test('Escape on a filtered overlay restores focus to the opener', async () => {
	/** [if] Escape hides a filtered overlay [then] the opener gets focus back. */
	const { trap, opener, search } = await openTrappedOverlay();
	assert.equal(fakeDocument.activeElement, search);

	trap.destroy();

	assert.equal(fakeDocument.activeElement, opener);
});

test('close button on a filtered overlay restores focus to the opener', async () => {
	/** [if] the X button hides a filtered overlay [then] the opener gets focus back. */
	const { trap, opener, close } = await openTrappedOverlay();
	close.focus();

	trap.destroy();

	assert.equal(fakeDocument.activeElement, opener);
});

test('a disconnected opener leaves no stale focused element', async () => {
	/** [if] the opener left the DOM before close [then] focus inside the overlay is blurred. */
	const { trap, opener, close, body } = await openTrappedOverlay();
	close.focus();
	opener.isConnected = false;

	trap.destroy();

	assert.equal(fakeDocument.activeElement, body);
	assert.notEqual(fakeDocument.activeElement, close);
	assert.notEqual(fakeDocument.activeElement, opener);
});
