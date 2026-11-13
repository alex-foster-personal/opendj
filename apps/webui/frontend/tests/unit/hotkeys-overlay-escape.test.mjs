/**
 * #12278: the hotkeys overlay filter's Escape sequence (A11Y-01 keyboard workflow).
 *
 * [if] ? opens the overlay [then] its filter receives focus
 * [if] filter text is present and Escape is pressed [then] the filter clears while the overlay remains open
 * [if] the empty filter receives Escape [then] the overlay closes
 *
 * Drives the real window listener from installHotkeysOverlayHotkeys over a fake window, one
 * bundle so the listener and the overlay store share one instance. Focus is a browser fact the
 * SSR renderer cannot show, so line one is pinned through markup plus the compiled client code;
 * tests/e2e/hotkeys-overlay.spec.ts presses the same keys in a real page.
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { after, before, test } from 'node:test';

import { compile } from 'svelte/compiler';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

let overlay;

before(async () => {
	overlay = await loadSvelteSsrModule(
		[
			"export { default as HotkeysOverlay } from '$lib/components/rb/hotkeys/HotkeysOverlay.svelte';",
			"export * from '$lib/components/rb/hotkeys/hotkeys-overlay.svelte';",
			"export { installHotkeysOverlayHotkeys } from '$lib/components/rb/hotkeys/install-hotkeys-overlay';",
			"export { render } from 'svelte/server';"
		].join('\n')
	);
});

after(() => overlay?.hideHotkeysOverlay());

//----- helpers

/** Stand-in for HTMLElement so the listener's instanceof check sees the overlay search box. */
class FakeHTMLElement {
	constructor(attrs) {
		this.attrs = attrs;
	}
	hasAttribute(name) {
		return name in this.attrs;
	}
	getAttribute(name) {
		return this.attrs[name] ?? null;
	}
}

/** Run `fn(press, search)` with the overlay's listeners installed on a fake window. */
function _withOverlayKeys(fn) {
	const listeners = {};
	const saved = { window: globalThis.window, HTMLElement: globalThis.HTMLElement };
	globalThis.window = {
		addEventListener: (type, handler) => (listeners[type] = handler),
		removeEventListener: () => {}
	};
	globalThis.HTMLElement = FakeHTMLElement;
	const search = Object.assign(new FakeHTMLElement({ 'data-hotkeys-overlay-search': 'true' }), {
		tagName: 'INPUT',
		type: 'search'
	});
	const page = Object.assign(new FakeHTMLElement({}), { tagName: 'DIV' });
	const uninstall = overlay.installHotkeysOverlayHotkeys();
	/** Dispatch one keydown at `target`; returns whether the listener claimed it. */
	const press = (key, target = search, over = {}) => {
		let prevented = false;
		listeners.keydown({
			key,
			code: '',
			metaKey: false,
			ctrlKey: false,
			altKey: false,
			shiftKey: false,
			repeat: false,
			target,
			preventDefault: () => (prevented = true),
			stopPropagation() {},
			...over
		});
		return prevented;
	};
	try {
		fn(press, { search, page });
	} finally {
		uninstall();
		overlay.hideHotkeysOverlay();
		Object.assign(globalThis, saved);
	}
}

const QUESTION = { code: 'Slash', shiftKey: true };

/** The open overlay's SSR markup for the current store state. */
function _markup() {
	return overlay.render(overlay.HotkeysOverlay, { props: {} }).body;
}

/** The filter input's opening tag in `body`. */
function _filterInput(body) {
	const tag = body.match(/<input[^>]*data-hotkeys-overlay-search="true"[^>]*>/);
	assert.ok(tag, `no overlay filter input in ${body.length} chars of markup`);
	return tag[0];
}

/** Client compile of the overlay view: what the browser runs when the dialog mounts. */
function _compiledView() {
	const filename = fileURLToPath(
		new URL('../../src/lib/components/rb/hotkeys/HotkeysOverlayView.svelte', import.meta.url)
	);
	return compile(readFileSync(filename, 'utf8'), { filename, generate: 'client' }).js.code;
}

//----- tests
// KEYB-14338 (#12278): Esc clears a non-empty filter first, then closes the overlay.

test('if ? opens the overlay [then] the open dialog leads with the filter and moves focus into it, [else stop].', () => {
	_withOverlayKeys((press, { page }) => {
		assert.equal(press('?', page, QUESTION), true, '? was not claimed by the overlay');
		assert.equal(overlay.isHotkeysOverlayOpen(), true);
		const body = _markup();
		const dialog = body.indexOf('role="dialog"');
		const input = _filterInput(body);
		const filter = body.indexOf(input);
		assert.ok(dialog >= 0 && filter > dialog, 'the filter is not inside the open dialog');
		const before = body.slice(dialog, filter);
		assert.doesNotMatch(
			before,
			/<(?:button|a|input|select|textarea)\b/,
			'a control precedes the filter'
		);
		assert.match(input, /aria-label="Filter hotkeys"/);
		const js = _compiledView();
		assert.match(
			js,
			/initialFocus:\s*['"]\.hk-search['"]/,
			'focusTrap does not start on the filter'
		);
		assert.match(js, /\.focus\(\)/, 'opening never calls focus on the filter');
	});
});

test('if the filter has text and Escape is pressed [then] the filter clears and the overlay stays open, [else stop].', () => {
	_withOverlayKeys((press, { search }) => {
		press('?', search, QUESTION);
		overlay.setHotkeysOverlayView('list');
		overlay.setHotkeysOverlayQuery('deck');
		assert.match(_filterInput(_markup()), /value="deck"/);
		assert.equal(press('Escape'), true, 'Escape fell through to the browser default');
		assert.equal(overlay.getHotkeysOverlayQuery(), '');
		assert.equal(
			overlay.isHotkeysOverlayOpen(),
			true,
			'Escape with filter text closed the overlay'
		);
		assert.equal(overlay.getHotkeysOverlayView(), 'list', 'clearing the filter reset the view');
		assert.doesNotMatch(_filterInput(_markup()), /value="deck"/);
	});
});

test('if the empty filter receives Escape [then] the overlay closes, and the full ? type Esc Esc sequence ends closed, [else stop].', () => {
	_withOverlayKeys((press, { search, page }) => {
		press('?', search, QUESTION);
		assert.equal(overlay.getHotkeysOverlayQuery(), '');
		assert.equal(press('Escape'), true);
		assert.equal(overlay.isHotkeysOverlayOpen(), false, 'Escape on an empty filter left it open');

		press('?', page, QUESTION);
		for (const key of 'loop') press(key, page);
		assert.equal(overlay.getHotkeysOverlayQuery(), 'loop', 'type-to-filter from the page');
		press('Escape', page);
		assert.equal(overlay.isHotkeysOverlayOpen(), true, 'first Escape closed instead of clearing');
		press('Escape', page);
		assert.equal(overlay.isHotkeysOverlayOpen(), false, 'second Escape did not close');
		assert.equal(press('Escape', page), false, 'a closed overlay still claims Escape');
	});
});
