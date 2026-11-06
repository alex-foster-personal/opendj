// requirement: UX-FLOAT-01
// requirement: LIBM-29
/**
 * Focus, Escape, and outside-press for pointFloatingAction menus shared by
 * RelocatePopoverView and TrackPlaylistsPopoverView (issue #8502).
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

globalThis.Node = globalThis.Node ?? class {};

let menu;

before(async () => {
	menu = await loadTypeScriptModule('src/lib/components/rb/browser/point-floating-menu.ts');
});

test('Escape preventDefault is called and onclose is called once', () => {
	let closed = 0;
	let prevented = 0;
	const event = {
		key: 'Escape',
		preventDefault() {
			prevented += 1;
		}
	};
	menu.closePointFloatingMenuOnEscape(event, () => {
		closed += 1;
	});
	assert.equal(prevented, 1);
	assert.equal(closed, 1);
});

test('other keys call neither preventDefault nor onclose', () => {
	let closed = 0;
	let prevented = 0;
	const event = {
		key: 'Enter',
		preventDefault() {
			prevented += 1;
		}
	};
	menu.closePointFloatingMenuOnEscape(event, () => {
		closed += 1;
	});
	assert.equal(prevented, 0);
	assert.equal(closed, 0);
});

test('outside pointer with a Node target closes the menu', () => {
	let closed = 0;
	const menuEl = {
		contains() {
			return false;
		}
	};
	menu.closePointFloatingMenuOnOutsidePointer({ target: new globalThis.Node() }, menuEl, () => {
		closed += 1;
	});
	assert.equal(closed, 1);
});

test('inside pointer does not close the menu', () => {
	let closed = 0;
	const menuEl = {
		contains() {
			return true;
		}
	};
	menu.closePointFloatingMenuOnOutsidePointer({ target: new globalThis.Node() }, menuEl, () => {
		closed += 1;
	});
	assert.equal(closed, 0);
});

test('a null menu does not close', () => {
	let closed = 0;
	menu.closePointFloatingMenuOnOutsidePointer({ target: new globalThis.Node() }, null, () => {
		closed += 1;
	});
	assert.equal(closed, 0);
});

test('a non-Node target does not close', () => {
	let closed = 0;
	const menuEl = {
		contains() {
			return false;
		}
	};
	menu.closePointFloatingMenuOnOutsidePointer({ target: {} }, menuEl, () => {
		closed += 1;
	});
	assert.equal(closed, 0);
});

test('focusPointFloatingMenu awaits then calls el.focus()', async () => {
	let focused = 0;
	const el = {
		focus() {
			focused += 1;
		}
	};
	await menu.focusPointFloatingMenu(el);
	assert.equal(focused, 1);
});

test('focusPointFloatingMenu(null) does not throw', async () => {
	await menu.focusPointFloatingMenu(null);
});
