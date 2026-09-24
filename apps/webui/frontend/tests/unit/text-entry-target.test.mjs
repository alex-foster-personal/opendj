/**
 * Issue #3528: one shared text-entry predicate for global shortcuts.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

function target(
	tagName,
	{ type = null, contentEditable = false, role = null } = {}
) {
	return {
		tagName,
		type,
		isContentEditable: contentEditable,
		getAttribute: (name) => (name === 'role' ? role : null)
	};
}

before(async () => {
	mod = await loadTypeScriptModule('src/lib/keyboard/text-entry-target.ts');
});

test('text-like inputs, textarea, select, and contenteditable are text entry', () => {
	for (const el of [
		target('INPUT', { type: '' }),
		target('INPUT', { type: 'text' }),
		target('INPUT', { type: 'search' }),
		target('INPUT', { type: 'email' }),
		target('INPUT', { type: 'url' }),
		target('INPUT', { type: 'tel' }),
		target('INPUT', { type: 'password' }),
		target('INPUT', { type: 'number' }),
		target('TEXTAREA'),
		target('SELECT'),
		target('DIV', { contentEditable: true }),
		target('DIV', { role: 'textbox' }),
		target('DIV', { role: 'searchbox' }),
		target('DIV', { role: 'combobox' })
	]) {
		assert.equal(mod.isTextEntryTarget(el), true, `${el.tagName} must suppress shortcuts`);
	}
});

test('buttons, library chrome, sliders, and non-text inputs are not text entry', () => {
	for (const el of [
		target('BUTTON'),
		target('INPUT', { type: 'checkbox' }),
		target('INPUT', { type: 'range' }),
		target('INPUT', { type: 'radio' }),
		target('DIV', { role: 'slider' }),
		target('TR', { role: 'row' }),
		target('DIV'),
		null,
		{}
	]) {
		assert.equal(mod.isTextEntryTarget(el), false, `${el?.tagName ?? 'null'} must allow shortcuts`);
	}
});
