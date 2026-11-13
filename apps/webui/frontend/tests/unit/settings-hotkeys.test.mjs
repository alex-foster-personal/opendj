/**
 * A11Y-01 settings chord from editable controls (issue #12269).
 *
 * [if] focus is in a text input on /performance [then] Cmd+, or Ctrl+, opens Settings [else stop].
 * [if] Settings is open and its search field has focus [then] Escape closes Settings [else stop].
 * [if] the settings chord is pressed [then] the browser default action does not run [else stop].
 *
 * Drives the real listener installSettingsHotkeys registers, through a fake
 * window that captures it, with fake keyboard events. The browser-level
 * counterpart is tests/e2e/settings-hotkeys.spec.ts.
 */
import assert from 'node:assert/strict';
import { afterEach, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const MODIFIERS = [
	{ name: 'Cmd', flags: { metaKey: true, ctrlKey: false } },
	{ name: 'Ctrl', flags: { metaKey: false, ctrlKey: true } }
];

let hotkeys;
let listeners;
let uninstall;

before(async () => {
	globalThis.window = {};
	hotkeys = await loadTypeScriptModule('tests/unit/fixtures/settings-hotkeys-entry.ts');
});

beforeEach(() => {
	listeners = [];
	globalThis.window = {
		addEventListener: (type, fn, capture) => listeners.push({ type, fn, capture }),
		removeEventListener: (type, fn) => {
			listeners = listeners.filter((entry) => entry.type !== type || entry.fn !== fn);
		}
	};
	hotkeys.closeSettings();
	uninstall = hotkeys.installSettingsHotkeys();
});

afterEach(() => {
	uninstall();
	hotkeys.closeSettings();
	delete globalThis.window;
});

function _textInput(className) {
	return { tagName: 'INPUT', type: 'text', className, isContentEditable: false };
}

function _press(init) {
	const event = {
		key: '',
		code: '',
		metaKey: false,
		ctrlKey: false,
		altKey: false,
		shiftKey: false,
		repeat: false,
		defaultPrevented: false,
		propagationStopped: false,
		preventDefault() {
			this.defaultPrevented = true;
		},
		stopPropagation() {
			this.propagationStopped = true;
		},
		...init
	};
	const keydown = listeners.filter((entry) => entry.type === 'keydown');
	assert.equal(
		keydown.length,
		1,
		'installSettingsHotkeys must register exactly one keydown listener'
	);
	assert.equal(keydown[0].capture, true, 'the settings listener must run in the capture phase');
	keydown[0].fn(event);
	return event;
}

function _chord(flags, target) {
	return _press({ key: ',', code: 'Comma', ...flags, target });
}

for (const { name, flags } of MODIFIERS) {
	test(`A11Y-01 ${name}+, opens Settings while a /performance text input has focus`, () => {
		/** [if] a library search input has focus and the chord is pressed [then] Settings opens, [else stop]. */
		assert.equal(hotkeys.isSettingsOpen(), false);
		_chord(flags, _textInput('rb-search-input'));
		assert.equal(hotkeys.isSettingsOpen(), true);
	});

	test(`A11Y-01 Escape closes Settings from its search field after ${name}+, opened it`, () => {
		/** [if] the chord opened Settings and Escape is pressed in its search field [then] Settings closes, [else stop]. */
		_chord(flags, _textInput('rb-search-input'));
		assert.equal(hotkeys.isSettingsOpen(), true);
		const escape = _press({ key: 'Escape', code: 'Escape', target: _textInput('so-search') });
		assert.equal(hotkeys.isSettingsOpen(), false);
		assert.equal(escape.defaultPrevented, true, 'Escape must not reach a browser default either');
	});

	test(`A11Y-01 ${name}+, prevents the browser default and stops propagation`, () => {
		/** [if] the chord is pressed from a text input [then] its default is prevented and it never reaches the input, [else stop]. */
		const opened = _chord(flags, _textInput('rb-search-input'));
		assert.equal(opened.defaultPrevented, true);
		assert.equal(opened.propagationStopped, true);
		const closed = _chord(flags, _textInput('so-search'));
		assert.equal(
			hotkeys.isSettingsOpen(),
			false,
			'the chord pressed again toggles Settings closed'
		);
		assert.equal(closed.defaultPrevented, true);
	});
}

test('A11Y-01 the chord ignores Alt and Shift variants and a bare comma', () => {
	/** [if] comma is pressed bare or with Alt or Shift added [then] Settings stays closed and the default runs, [else stop]. */
	for (const extra of [{}, { metaKey: true, altKey: true }, { ctrlKey: true, shiftKey: true }]) {
		const event = _press({
			key: ',',
			code: 'Comma',
			...extra,
			target: _textInput('rb-search-input')
		});
		assert.equal(hotkeys.isSettingsOpen(), false);
		assert.equal(event.defaultPrevented, false);
	}
});

test('A11Y-01 Escape with Settings closed leaves the default alone', () => {
	/** [if] Escape is pressed in a text input while Settings is closed [then] nothing is prevented, [else stop]. */
	const escape = _press({ key: 'Escape', code: 'Escape', target: _textInput('rb-search-input') });
	assert.equal(escape.defaultPrevented, false);
	assert.equal(hotkeys.isSettingsOpen(), false);
});
