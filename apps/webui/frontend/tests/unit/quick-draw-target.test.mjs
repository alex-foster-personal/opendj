/**
 * Parity matrix row 30: quick-draw leaves right-clicks in text fields to the
 * platform edit menu (Cut, Copy, Paste, Select All).
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

function target(tagName, { inPerfRoot = true, type = null, contentEditable = false, role = null } = {}) {
	return {
		tagName,
		type,
		isContentEditable: contentEditable,
		getAttribute: (name) => (name === 'role' ? role : null),
		closest: (selector) => (selector === '.perf-root' && inPerfRoot ? {} : null)
	};
}

before(async () => {
	mod = await loadTypeScriptModule('src/lib/components/rb/quick-draw-target.ts');
});

test('if quick-draw takes a right-click in the library search box then Paste by right-click is lost', () => {
	for (const el of [
		target('INPUT', { type: 'search' }),
		target('INPUT', { type: 'text' }),
		target('TEXTAREA'),
		target('DIV', { contentEditable: true }),
		target('DIV', { role: 'textbox' })
	]) {
		assert.equal(mod.quickDrawClaimsContextMenu(el), false, `${el.tagName} ${el.type ?? ''}`);
	}
});

test('if quick-draw stops taking right-clicks on decks and rows then the performance menu is gone', () => {
	for (const el of [target('DIV'), target('CANVAS'), target('TR'), target('BUTTON'), target('INPUT', { type: 'range' })]) {
		assert.equal(mod.quickDrawClaimsContextMenu(el), true, el.tagName);
	}
});

test('if quick-draw claims a right-click outside .perf-root then other pages lose their menu', () => {
	assert.equal(mod.quickDrawClaimsContextMenu(target('DIV', { inPerfRoot: false })), false);
});
