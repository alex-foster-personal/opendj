// requirement: KEYB-13954
/**
 * Issue #12283: the global "/" and "?" overlay gestures ignore keydown events
 * sent during an IME composition (isComposing, or the legacy keyCode 229), so a
 * candidate sequence cannot open the overlay or lose typed text. Once the
 * composition ends the gestures behave as before.
 */

import assert from 'node:assert/strict';
import { after, afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

class FakeElement {
	constructor(tagName) {
		this.tagName = tagName;
		this.type = '';
		this.isContentEditable = false;
	}
	getAttribute() {
		return null;
	}
	hasAttribute() {
		return false;
	}
}

function fakeWindow() {
	const listeners = { keydown: [], keyup: [], blur: [] };
	return {
		listeners,
		addEventListener(type, fn) {
			listeners[type].push(fn);
		},
		removeEventListener(type, fn) {
			listeners[type] = listeners[type].filter((f) => f !== fn);
		}
	};
}

function key(type, init) {
	const event = {
		type,
		key: init.key,
		code: init.code ?? '',
		keyCode: init.keyCode ?? 0,
		isComposing: init.isComposing ?? false,
		shiftKey: init.shiftKey ?? false,
		metaKey: false,
		ctrlKey: false,
		altKey: false,
		repeat: false,
		target: new FakeElement('BODY'),
		defaultPrevented: false,
		preventDefault() {
			this.defaultPrevented = true;
		},
		stopPropagation() {}
	};
	for (const fn of globalThis.window.listeners[type]) fn(event);
	return event;
}

const SLASH = { key: '/', code: 'Slash' };
const QUESTION = { key: '?', code: 'Slash', shiftKey: true };
const COMPOSING_SHAPES = [{ isComposing: true }, { keyCode: 229 }];

let mod;
let uninstall = null;
const savedHTMLElement = globalThis.HTMLElement;
const savedWindow = globalThis.window;

before(async () => {
	globalThis.HTMLElement = FakeElement;
	globalThis.window = fakeWindow();
	mod = await loadTypeScriptModule('tests/unit/fixtures/hotkeys-overlay-text-entry-entry.ts');
});

afterEach(() => {
	uninstall?.();
	uninstall = null;
	mod.hideHotkeysOverlay();
	globalThis.window = fakeWindow();
});

after(() => {
	globalThis.HTMLElement = savedHTMLElement;
	globalThis.window = savedWindow;
});

function install() {
	uninstall = mod.installHotkeysOverlayHotkeys();
}

test('[if] a / keydown is composing (isComposing or keyCode 229) [then] the overlay stays hidden and the key is not consumed, [else stop].', () => {
	install();
	for (const shape of COMPOSING_SHAPES) {
		const down = key('keydown', { ...SLASH, ...shape });
		assert.equal(
			down.defaultPrevented,
			false,
			`composing / (${JSON.stringify(shape)}) must reach the IME`
		);
		assert.equal(
			mod.isHotkeysOverlayOpen(),
			false,
			`composing / (${JSON.stringify(shape)}) must not show the overlay`
		);
		key('keyup', SLASH);
	}
});

test('[if] a ? keydown is composing (isComposing or keyCode 229) [then] the overlay does not toggle either way, [else stop].', () => {
	install();
	for (const shape of COMPOSING_SHAPES) {
		const down = key('keydown', { ...QUESTION, ...shape });
		assert.equal(
			down.defaultPrevented,
			false,
			`composing ? (${JSON.stringify(shape)}) must reach the IME`
		);
		assert.equal(
			mod.isHotkeysOverlayOpen(),
			false,
			`composing ? (${JSON.stringify(shape)}) must not open the overlay`
		);
	}
	mod.showHotkeysOverlay();
	for (const shape of COMPOSING_SHAPES) {
		key('keydown', { ...QUESTION, ...shape });
		assert.equal(
			mod.isHotkeysOverlayOpen(),
			true,
			`composing ? (${JSON.stringify(shape)}) must not close the overlay`
		);
	}
});

test('[if] composition ends and ? is then pressed normally [then] the overlay toggles open and closed as before, [else stop].', () => {
	install();
	key('keydown', { ...QUESTION, isComposing: true });
	key('keydown', { ...QUESTION, keyCode: 229 });
	assert.equal(mod.isHotkeysOverlayOpen(), false);
	const open = key('keydown', QUESTION);
	assert.equal(open.defaultPrevented, true, 'a normal ? after composition is the overlay gesture');
	assert.equal(mod.isHotkeysOverlayOpen(), true, 'a normal ? after composition opens the overlay');
	key('keydown', QUESTION);
	assert.equal(mod.isHotkeysOverlayOpen(), false, 'a second normal ? closes it');
});
