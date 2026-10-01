/**
 * Issue #3980: shared M / Cmd+Shift+M routing for comment pins.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let routing;

class FakeElement {}
class FakeHTMLElement extends FakeElement {}

function target(tagName, overrides = {}) {
	const el = new FakeHTMLElement();
	el.tagName = tagName;
	el.type = overrides.type ?? null;
	el.isContentEditable = overrides.contentEditable ?? false;
	el.getAttribute = (name) => (name === 'role' ? (overrides.role ?? null) : null);
	el.hasAttribute = (name) =>
		name === 'data-hotkey-pointer-only' ? overrides.pointerOnly === true : false;
	return el;
}

function key(overrides = {}) {
	return {
		key: overrides.key ?? 'm',
		target: overrides.target ?? target('BODY'),
		metaKey: overrides.metaKey ?? false,
		ctrlKey: overrides.ctrlKey ?? false,
		altKey: overrides.altKey ?? false,
		shiftKey: overrides.shiftKey ?? false
	};
}

before(async () => {
	globalThis.Element = FakeElement;
	globalThis.HTMLElement = FakeHTMLElement;
	routing = await loadTypeScriptModule('src/lib/rb/comment-pin-hotkey-routing.ts');
});

test('plain m arms on button and library row', () => {
	for (const focus of [target('BUTTON'), target('TR', { role: 'row' })]) {
		assert.equal(routing.resolveCommentPinHotkey(key({ target: focus })), 'arm');
	}
});

test('plain m does not arm in text input', () => {
	assert.equal(
		routing.resolveCommentPinHotkey(key({ target: target('INPUT', { type: 'text' }) })),
		null
	);
});

test('plain m arms on pointer-only canvas attribute', () => {
	assert.equal(
		routing.resolveCommentPinHotkey(key({ target: target('CANVAS', { pointerOnly: true }) })),
		'arm'
	);
});

test('FB-19: Cmd+Shift+M arms even in textarea', () => {
	assert.equal(
		routing.resolveCommentPinHotkey(
			key({
				target: target('TEXTAREA'),
				metaKey: true,
				shiftKey: true
			})
		),
		'arm'
	);
});

// pin 28a5effd: "more places in UI that m doesn't work that ARE NOT text
// entry". Two were left: a focused <select>, and anything at all while the
// Settings overlay was open (the router took a `settingsOpen` veto).
//
// - if a focused <select> swallows m then the dropdowns in Settings and the
//   mixer are dead spots for the comment hotkey -> broken
// - if the Settings overlay vetoes m then no pin can be placed on Settings,
//   which is exactly where most of the dropdowns live -> broken
// - if m arms while typing in a text input, textarea or contenteditable then
//   the letter is stolen from the text -> broken (the overshoot)
test('pin 28a5effd: plain m arms on a focused select', () => {
	assert.equal(routing.resolveCommentPinHotkey(key({ target: target('SELECT') })), 'arm');
	assert.equal(routing.resolveCommentPinHotkey(key({ key: 'M', target: target('SELECT') })), 'arm');
});

test('pin 28a5effd: the router takes no Settings veto', () => {
	assert.equal(
		routing.resolveCommentPinHotkey.length,
		1,
		'the router must take the key event only: a second options argument is how the veto got in'
	);
});

test('pin 28a5effd: plain m still never arms while typing', () => {
	const typing = [
		target('INPUT', { type: 'text' }),
		target('INPUT', { type: 'search' }),
		target('INPUT', { type: 'number' }),
		target('INPUT', { type: '' }),
		target('TEXTAREA'),
		target('DIV', { contentEditable: true }),
		target('DIV', { role: 'textbox' }),
		target('INPUT', { role: 'combobox', type: 'text' })
	];
	for (const focus of typing) {
		assert.equal(routing.resolveCommentPinHotkey(key({ target: focus })), null);
	}
});

test('pin 28a5effd: the installer does not consult the Settings overlay', async () => {
	const { readFileSync } = await import('node:fs');
	const src = readFileSync(
		new URL('../../src/lib/rb/comment-pin-hotkeys.ts', import.meta.url),
		'utf8'
	);
	assert.doesNotMatch(src, /isSettingsOpen/);
});
