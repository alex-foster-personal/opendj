/**
 * /performance keyboard shortcut target guard.
 *
 * Regression lines:
 * - if Tab is handled while a native control has focus then keyboard focus is trapped
 * - if Space is handled while a native control has focus then that control cannot activate
 * - if non-interactive page chrome suppresses Tab or Space then the intended performance hotkeys stop working
 */

import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

class FakeElement {}
class FakeHTMLElement extends FakeElement {}

let mod;

function element(tagName, { contentEditable = false, href = false, role = null, tabindex = false } = {}) {
	const target = new FakeHTMLElement();
	target.tagName = tagName;
	target.isContentEditable = contentEditable;
	target.getAttribute = (name) => (name === 'role' ? role : null);
	target.hasAttribute = (name) => (name === 'href' ? href : name === 'tabindex' ? tabindex : false);
	return target;
}

function svgElement({ role = null, tabindex = false } = {}) {
	const target = new FakeElement();
	target.getAttribute = (name) => (name === 'role' ? role : null);
	target.hasAttribute = (name) => (name === 'tabindex' ? tabindex : false);
	return target;
}

before(async () => {
	globalThis.Element = FakeElement;
	globalThis.HTMLElement = FakeHTMLElement;
	mod = await loadTypeScriptModule('src/lib/rb/performance-hotkeys-target.ts');
});

test('native focusable controls retain Tab focus movement and Space activation', () => {
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
		element('DIV', { tabindex: true }),
		svgElement({ role: 'button' }),
		svgElement({ tabindex: true }),
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

test('the global performance listener uses the native-control guard before handling a shortcut', async () => {
	const source = await readFile('src/lib/rb/performance-hotkeys.ts', 'utf8');
	assert.match(source, /if \(isNativeInteractiveTarget\(e\.target\) \|\| e\.metaKey/);
});
