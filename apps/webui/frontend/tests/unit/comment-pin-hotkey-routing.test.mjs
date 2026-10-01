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

test('Cmd+Shift+M arms even in textarea', () => {
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

test('settings open blocks arming', () => {
	assert.equal(
		routing.resolveCommentPinHotkey(key(), { settingsOpen: true }),
		null
	);
});
