// Parity row 30: the right-click edit menu WKWebView gives text fields.
import * as assert from 'node:assert/strict';
import { test } from 'node:test';

import { type EditContextParams, editContextMenuTemplate } from '../src/context-menu';

const ALL_ON = {
	canUndo: true,
	canRedo: true,
	canCut: true,
	canCopy: true,
	canPaste: true,
	canDelete: true,
	canSelectAll: true,
	canEditRichly: false
};

function params(overrides: Partial<EditContextParams>): EditContextParams {
	return { isEditable: false, selectionText: '', editFlags: ALL_ON, ...overrides };
}

test('if a right-click in a text field offers no cut, copy, paste and select all then the edit menu is missing', () => {
	const template = editContextMenuTemplate(params({ isEditable: true }));
	assert.ok(template !== null);
	assert.deepEqual(
		template.map((item) => item.role ?? item.type),
		['cut', 'copy', 'paste', 'separator', 'selectAll']
	);
	assert.ok(template.every((item) => item.type === 'separator' || item.enabled === true));
});

test('if an empty text field offers cut or copy as enabled then the menu ignores what Chromium reports', () => {
	const template = editContextMenuTemplate(
		params({ isEditable: true, editFlags: { ...ALL_ON, canCut: false, canCopy: false, canSelectAll: false } })
	);
	const enabled = Object.fromEntries((template ?? []).filter((item) => item.role !== undefined).map((item) => [item.role, item.enabled]));
	assert.deepEqual(enabled, { cut: false, copy: false, paste: true, selectAll: false });
});

test('if selected page text outside a field offers paste then the menu is not copy-only', () => {
	const template = editContextMenuTemplate(params({ selectionText: 'Artist - Title' }));
	assert.deepEqual(
		template?.map((item) => item.role),
		['copy']
	);
});

test('if a right-click on the decks or waveform shows a menu then the page loses its own right-click', () => {
	assert.equal(editContextMenuTemplate(params({})), null);
	assert.equal(editContextMenuTemplate(params({ selectionText: '  \n' })), null);
});
