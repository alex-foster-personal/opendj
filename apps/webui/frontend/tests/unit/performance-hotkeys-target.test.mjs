/**
 * isNativeInteractiveTarget (issue #3528 review r3549, P1/P3): the guard
 * performance-shortcut-routing.ts's Tab branch uses to tell real keyboard
 * focus traversal apart from page chrome. Restored after a P3 finding on
 * this PR: an earlier revision deleted this whole matrix without deleting
 * the module, leaving it both dead code and an uncovered guard. It has a
 * live consumer again (the Tab branch only - Space/M/loop-resize stay on
 * the narrower isTextEntryTarget predicate per the amended A11Y-01).
 *
 * Regression lines:
 * - if Tab fires while a native control has focus then keyboard focus is trapped
 * - if non-interactive page chrome suppresses Tab then the next-only-filter binding stops working
 * - if a pointer-only control marked with the explicit passthrough attribute
 *   (WaveRow's waveform-seek canvas: role="slider", tabindex="-1", no
 *   keydown handler of its own) is incidentally focused by a click, then
 *   Tab must still be free to move focus off it (pin 18627f290052)
 * - if a NEGATIVE tabindex alone (with no passthrough marker) were treated
 *   as "not interactive" then a programmatically focused <input>/<button>,
 *   a contenteditable node, or a roving-tabindex ARIA widget
 *   (role="textbox"/"listbox" etc, tabindex="-1") would have its real
 *   keyboard behavior swallowed by the global hotkeys instead - r3921-review
 *   P1 BLOCKING, caught before merge
 */

import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

class FakeElement {}
class FakeHTMLElement extends FakeElement {}

let mod;

// tabindex is the real attribute VALUE (string) or `false` for absent.
// pointerOnly sets the explicit data-hotkey-pointer-only opt-out marker -
// the ONLY thing that should ever suppress an otherwise-interactive element,
// never a negative tabindex by itself.
function element(
	tagName,
	{ contentEditable = false, href = false, role = null, tabindex = false, pointerOnly = false } = {}
) {
	const target = new FakeHTMLElement();
	target.tagName = tagName;
	target.isContentEditable = contentEditable;
	target.getAttribute = (name) => (name === 'role' ? role : name === 'tabindex' ? tabindex : null);
	target.hasAttribute = (name) =>
		name === 'href'
			? href
			: name === 'tabindex'
				? tabindex !== false
				: name === 'data-hotkey-pointer-only'
					? pointerOnly
					: false;
	return target;
}

function svgElement({ role = null, tabindex = false, pointerOnly = false } = {}) {
	const target = new FakeElement();
	target.getAttribute = (name) => (name === 'role' ? role : name === 'tabindex' ? tabindex : null);
	target.hasAttribute = (name) =>
		name === 'tabindex' ? tabindex !== false : name === 'data-hotkey-pointer-only' ? pointerOnly : false;
	return target;
}

before(async () => {
	globalThis.Element = FakeElement;
	globalThis.HTMLElement = FakeHTMLElement;
	mod = await loadTypeScriptModule('src/lib/rb/performance-hotkeys-target.ts');
});

test('native focusable controls retain Tab focus movement', () => {
	for (const target of [
		element('BUTTON'),
		element('INPUT'),
		element('TEXTAREA'),
		element('SELECT'),
		element('SUMMARY'),
		element('A', { href: true }),
		element('DIV', { role: 'button' }),
		element('DIV', { role: 'checkbox' }),
		element('DIV', { role: 'menuitem' }),
		element('DIV', { role: 'option' }),
		element('DIV', { role: 'radio' }),
		element('DIV', { role: 'slider' }),
		element('DIV', { role: 'spinbutton' }),
		element('DIV', { role: 'tab' }),
		element('DIV', { tabindex: '0' }),
		svgElement({ role: 'button' }),
		svgElement({ tabindex: '0' }),
		element('DIV', { contentEditable: true })
	]) {
		assert.equal(
			mod.isNativeInteractiveTarget(target),
			true,
			`${target.tagName} must keep its browser keyboard behavior`
		);
	}
});

test('non-interactive page chrome remains eligible for performance hotkeys', () => {
	assert.equal(mod.isNativeInteractiveTarget(element('DIV')), false);
	assert.equal(mod.isNativeInteractiveTarget(null), false);
	assert.equal(mod.isNativeInteractiveTarget({}), false);
});

test('a NEGATIVE tabindex alone keeps native keyboard behavior protected (P1 BLOCKING regression)', () => {
	// r3921-review: an earlier version of this guard treated ANY negative
	// tabindex as proof the element is non-interactive, short-circuiting
	// before the role/tag/contentEditable checks ran at all. That silently
	// re-broke every one of these - all common shapes for a
	// programmatically-focused (not Tab-reached) element that still owns
	// real keyboard behavior: a modal focus trap, a roving-tabindex ARIA
	// widget, a script-focused native control, an editable node.
	for (const target of [
		element('INPUT', { tabindex: '-1' }),
		element('BUTTON', { tabindex: '-1' }),
		element('DIV', { contentEditable: true, tabindex: '-1' }),
		element('DIV', { role: 'textbox', tabindex: '-1' }),
		element('DIV', { role: 'listbox', tabindex: '-1' })
	]) {
		assert.equal(
			mod.isNativeInteractiveTarget(target),
			true,
			`${target.tagName} tabindex="-1" must still keep its browser keyboard behavior`
		);
	}
});

test('only the explicit pointer-only marker opts a control out of the Tab guard', () => {
	// WaveRow.svelte's waveform-seek canvas: role="slider" + tabindex="-1" +
	// data-hotkey-pointer-only, no onkeydown of its own. It can still become
	// document.activeElement incidentally (its pointerdown handler skips
	// preventDefault on an empty deck or a pending command) - the marker is
	// what tells the guard this specific element owns no keyboard behavior
	// despite its role, NOT the bare fact that its tabindex is negative.
	assert.equal(
		mod.isNativeInteractiveTarget(element('CANVAS', { role: 'slider', tabindex: '-1', pointerOnly: true })),
		false,
		'the marked pointer-only canvas must remain eligible for performance hotkeys'
	);
	// The exact same shape WITHOUT the marker is the pairing that proves the
	// opt-out is marker-scoped, not tabindex-sign-scoped: a bare role="slider"
	// tabindex="-1" element (e.g. some other future widget) keeps the
	// conservative "assume it owns its keys" default.
	assert.equal(
		mod.isNativeInteractiveTarget(element('CANVAS', { role: 'slider', tabindex: '-1' })),
		true,
		'an unmarked role="slider" tabindex="-1" element must still be treated as native-interactive'
	);
});
