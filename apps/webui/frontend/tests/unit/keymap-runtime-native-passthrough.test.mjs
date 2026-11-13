// requirement: KEYB-14109
/**
 * Issue #14100: a user remap that binds plain Enter must not take Enter from a focused native
 * control, the same rule plain Space already follows. Modifier chords (Cmd+Enter) still route.
 * The real keymap runtime gate runs under a user remap; only the focused element is synthesized.
 *
 * Regression lines:
 * - if a remap bound to plain Enter runs while a button has focus then broken
 * - if a remap bound to plain Enter does not run from the page body then broken
 * - if a remap bound to Cmd+Enter stops running while a button has focus then broken
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';

const ENTRY = [
	"export { handleKeymapKeydown, refreshKeyboardPrefFromServer } from '$lib/keyboard/keymap-runtime.svelte';",
	"export { chordFromEvent } from '$lib/keyboard/keymap';",
	"export { isNativeInteractiveTarget } from '$lib/rb/performance-hotkeys-target';",
	"export { uiPrefs } from '$lib/rb/performance-ipc.svelte';"
].join('\n');

let mod;
const saved = { Element: globalThis.Element, HTMLElement: globalThis.HTMLElement };

// Stand-ins for the DOM classes the runtime tests with instanceof; no DOM here.
class FakeElement {
	constructor(tagName, attrs = {}) {
		this.tagName = tagName;
		this.attrs = attrs;
		this.isContentEditable = false;
	}
	hasAttribute(name) {
		return Object.hasOwn(this.attrs, name);
	}
	getAttribute(name) {
		return this.hasAttribute(name) ? this.attrs[name] : null;
	}
	closest() {
		return null;
	}
}
class FakeHTMLElement extends FakeElement {}

before(async () => {
	mod = await loadSvelteSsrModule(ENTRY);
	globalThis.Element = FakeElement;
	globalThis.HTMLElement = FakeHTMLElement;
});

after(() => {
	globalThis.Element = saved.Element;
	globalThis.HTMLElement = saved.HTMLElement;
});

const button = () => new FakeHTMLElement('BUTTON', { type: 'button' });
const body = () => new FakeHTMLElement('BODY');

function enterEvent(target, { metaKey = false } = {}) {
	return {
		code: 'Enter',
		key: 'Enter',
		target,
		metaKey,
		ctrlKey: false,
		altKey: false,
		shiftKey: false
	};
}

/**
 * Press `event` through the real runtime with transport.toggle remapped to `event`'s own chord.
 * Returns whether the runtime claimed the key, cancelled it, and ran the remapped command.
 */
function pressUnderRemap(event) {
	const chord = mod.chordFromEvent(event);
	const before = mod.uiPrefs.keyboard;
	let prevented = false;
	let ran = 0;
	const legacy = { toggleRecentPlay: () => (ran += 1) };
	try {
		mod.uiPrefs.keyboard = { preset: 'opendj', overrides: { 'transport.toggle': [chord] } };
		mod.refreshKeyboardPrefFromServer();
		const keydown = {
			...event,
			repeat: false,
			timeStamp: 0,
			preventDefault: () => (prevented = true)
		};
		const claimed = mod.handleKeymapKeydown(keydown, legacy);
		return { chord, claimed, prevented, ran };
	} finally {
		mod.uiPrefs.keyboard = before;
		mod.refreshKeyboardPrefFromServer();
	}
}

/** [if] a user remap binds plain Enter and a button has focus [then] Enter reaches the button and the command does not run, [else stop]. */
test('a remap bound to plain Enter leaves Enter to a focused button', () => {
	const event = enterEvent(button());
	assert.equal(mod.isNativeInteractiveTarget(event.target), true, 'the button owns its keys');
	const verdict = pressUnderRemap(event);
	assert.equal(verdict.chord, 'Enter');
	assert.deepEqual(
		{ claimed: verdict.claimed, prevented: verdict.prevented, ran: verdict.ran },
		{ claimed: false, prevented: false, ran: 0 }
	);
});

/** [if] a user remap binds plain Enter and focus is on the page body [then] the remapped command runs, [else stop]. */
test('a remap bound to plain Enter runs from the page body', () => {
	const event = enterEvent(body());
	assert.equal(mod.isNativeInteractiveTarget(event.target), false, 'the body is page chrome');
	const verdict = pressUnderRemap(event);
	assert.deepEqual(
		{ claimed: verdict.claimed, prevented: verdict.prevented, ran: verdict.ran },
		{ claimed: true, prevented: true, ran: 1 }
	);
});

/** [if] Cmd+Enter is bound and a button has focus [then] the bound command still runs, [else stop]. */
test('a remap bound to Cmd+Enter still runs while a button has focus', () => {
	const verdict = pressUnderRemap(enterEvent(button(), { metaKey: true }));
	assert.notEqual(verdict.chord, 'Enter', 'the modifier is part of the chord');
	assert.match(verdict.chord, /\+Enter$/);
	assert.deepEqual(
		{ claimed: verdict.claimed, prevented: verdict.prevented, ran: verdict.ran },
		{ claimed: true, prevented: true, ran: 1 }
	);
});
