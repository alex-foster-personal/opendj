// requirement: KEYB-13940
/**
 * Issue #12273: the hotkeys overlay is safe during text entry.
 *
 * Direct overlay commands (show / hide, what a typed IPC or palette caller
 * runs) and physical shortcut routing (the window key listeners) are tested
 * independently: the command tests install no listener, the routing tests
 * drive keys only.
 */

import assert from 'node:assert/strict';
import { after, afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

class FakeElement {
	constructor(tagName, { type = '', attrs = {} } = {}) {
		this.tagName = tagName;
		this.type = type;
		this.value = '';
		this.isContentEditable = false;
		this.attrs = attrs;
		this.dispatched = [];
	}
	getAttribute(name) {
		return name in this.attrs ? this.attrs[name] : null;
	}
	hasAttribute(name) {
		return name in this.attrs;
	}
	dispatchEvent(event) {
		this.dispatched.push(event);
		return true;
	}
}

function fakeWindow() {
	const listeners = { keydown: [], keyup: [], blur: [] };
	return {
		listeners,
		dispatched: [],
		addEventListener(type, fn) {
			listeners[type].push(fn);
		},
		removeEventListener(type, fn) {
			listeners[type] = listeners[type].filter((f) => f !== fn);
		},
		dispatchEvent(event) {
			this.dispatched.push(event);
			return true;
		}
	};
}

function key(type, init, target) {
	const event = {
		type,
		key: init.key,
		code: init.code ?? '',
		shiftKey: init.shiftKey ?? false,
		metaKey: false,
		ctrlKey: false,
		altKey: false,
		repeat: false,
		target,
		defaultPrevented: false,
		propagationStopped: false,
		preventDefault() {
			this.defaultPrevented = true;
		},
		stopPropagation() {
			this.propagationStopped = true;
		}
	};
	for (const fn of globalThis.window.listeners[type]) fn(event);
	return event;
}

const SLASH = { key: '/', code: 'Slash' };
const QUESTION = { key: '?', code: 'Slash', shiftKey: true };

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

test('[if] a text field has focus [then] / and ? keep text-entry behaviour and do not drive the overlay, [else stop].', () => {
	install();
	const field = new FakeElement('INPUT', { type: 'text' });
	for (const init of [SLASH, QUESTION]) {
		const down = key('keydown', init, field);
		key('keyup', init, field);
		assert.equal(down.defaultPrevented, false, `${init.key} must reach the field`);
		assert.equal(down.propagationStopped, false, `${init.key} must reach the field's own handlers`);
		assert.equal(
			mod.isHotkeysOverlayOpen(),
			false,
			`${init.key} in a field must not open the overlay`
		);
	}
	mod.showHotkeysOverlay();
	for (const init of [SLASH, QUESTION]) {
		const down = key('keydown', init, field);
		key('keyup', init, field);
		assert.equal(
			down.defaultPrevented,
			false,
			`${init.key} must still reach the field with the overlay open`
		);
		assert.equal(
			mod.isHotkeysOverlayOpen(),
			true,
			`${init.key} in a field must not toggle the overlay off`
		);
	}
	assert.equal(mod.getHotkeysOverlayQuery(), '');
});

test('[if] typed IPC opens the overlay while a text field has focus [then] the field value and its keys are untouched, [else stop].', () => {
	install();
	const field = new FakeElement('TEXTAREA');
	field.value = 'set notes';
	mod.showHotkeysOverlay();
	assert.equal(mod.isHotkeysOverlayOpen(), true);
	assert.equal(field.value, 'set notes');
	assert.deepEqual(field.dispatched, []);
	assert.deepEqual(globalThis.window.dispatched, []);
	const typed = key('keydown', { key: 'x', code: 'KeyX' }, field);
	const erased = key('keydown', { key: 'Backspace', code: 'Backspace' }, field);
	assert.equal(typed.defaultPrevented, false, 'a printable key in the field must not be consumed');
	assert.equal(erased.defaultPrevented, false, 'Backspace in the field must not be consumed');
	assert.equal(
		mod.getHotkeysOverlayQuery(),
		'',
		'field keys must not leak into the overlay filter'
	);
});

test('[if] typed IPC closes the overlay [then] no key event is synthesized and a later keypress is not blocked, [else stop].', () => {
	install();
	const body = new FakeElement('BODY');
	mod.showHotkeysOverlay();
	mod.hideHotkeysOverlay();
	assert.equal(mod.isHotkeysOverlayOpen(), false);
	assert.deepEqual(globalThis.window.dispatched, []);
	const printable = key('keydown', { key: 'a', code: 'KeyA' }, body);
	const escape = key('keydown', { key: 'Escape', code: 'Escape' }, body);
	assert.equal(printable.defaultPrevented, false, 'a closed overlay must not consume a later key');
	assert.equal(escape.defaultPrevented, false, 'a closed overlay must not consume a later Esc');
	const toggle = key('keydown', QUESTION, body);
	assert.equal(toggle.defaultPrevented, true);
	assert.equal(mod.isHotkeysOverlayOpen(), true, 'a later ? must still open the overlay');
	key('keydown', { key: 'Escape', code: 'Escape' }, body);
	key('keydown', SLASH, body);
	assert.equal(mod.isHotkeysOverlayOpen(), true, 'a later / hold must still show the overlay');
	key('keyup', SLASH, body);
	assert.equal(mod.isHotkeysOverlayOpen(), false);
});

test('[if] direct overlay commands run with no key listener installed [then] show, hide and toggle drive the overlay alone, [else stop].', () => {
	assert.equal(globalThis.window.listeners.keydown.length, 0);
	mod.showHotkeysOverlay();
	mod.setHotkeysOverlayQuery('load');
	assert.equal(mod.isHotkeysOverlayOpen(), true);
	mod.hideHotkeysOverlay();
	assert.equal(mod.isHotkeysOverlayOpen(), false);
	assert.equal(mod.getHotkeysOverlayQuery(), '', 'hide resets the filter');
	mod.toggleHotkeysOverlay();
	assert.equal(mod.isHotkeysOverlayOpen(), true);
	mod.toggleHotkeysOverlay();
	assert.equal(mod.isHotkeysOverlayOpen(), false);
	assert.deepEqual(globalThis.window.dispatched, []);
});

test('[if] keys are typed outside text entry [then] / holds, ? toggles and printable keys filter the open overlay, [else stop].', () => {
	install();
	const body = new FakeElement('BODY');
	const search = new FakeElement('INPUT', {
		type: 'search',
		attrs: { 'data-hotkeys-overlay-search': 'true' }
	});
	key('keydown', SLASH, body);
	assert.equal(mod.isHotkeysOverlayOpen(), true);
	key('keyup', SLASH, body);
	assert.equal(mod.isHotkeysOverlayOpen(), false);
	key('keydown', QUESTION, body);
	assert.equal(mod.isHotkeysOverlayOpen(), true);
	const typed = key('keydown', { key: 'p', code: 'KeyP' }, body);
	assert.equal(typed.defaultPrevented, true);
	assert.equal(mod.getHotkeysOverlayQuery(), 'p');
	const inSearch = key('keydown', { key: 'q', code: 'KeyQ' }, search);
	assert.equal(inSearch.defaultPrevented, false, 'the overlay search box types for itself');
	key('keydown', QUESTION, search);
	assert.equal(mod.isHotkeysOverlayOpen(), false, '? from the overlay search still toggles');
});
