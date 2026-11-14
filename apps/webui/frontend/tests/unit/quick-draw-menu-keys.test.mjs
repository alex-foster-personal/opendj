// KEYB-14648 (#14632): the /performance quick draw menu follows the WAI-ARIA menu key pattern (mimicked).
// The component flattens its items (contextual actions, quick-draw roots, open leaf) into one list
// and asks quick-draw-menu-keys.ts what a key does; isMenuItemKeyTarget keeps page hotkeys off them.
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

class FakeElement {}

let keys;
let target;

function menuItem(role) {
	const el = new FakeElement();
	el.getAttribute = (name) => (name === 'role' ? role : null);
	el.hasAttribute = () => false;
	return el;
}

before(async () => {
	globalThis.Element = FakeElement;
	globalThis.HTMLElement = class extends FakeElement {};
	keys = await loadTypeScriptModule('src/lib/components/rb/quick-draw-menu-keys.ts');
	target = await loadTypeScriptModule('src/lib/rb/performance-hotkeys-target.ts');
});

// [if] the menu opens [then] focus goes to its first enabled item, skipping inert rungs, [else stop].
test('KEYB-14648: open focuses the first enabled menu item', () => {
	assert.equal(keys.firstEnabledIndex([false, false, true, true]), 2);
	assert.equal(keys.firstEnabledIndex([true, false]), 0);
	assert.equal(keys.firstEnabledIndex([false, false]), -1);
});

// [if] ArrowDown is on the last item [then] focus wraps to the first; ArrowUp on the first wraps to the last, [else stop].
test('KEYB-14648: ArrowDown and ArrowUp move with wrap and skip disabled items', () => {
	const enabled = [true, false, true, true, false];
	assert.deepEqual(keys.quickDrawMenuKeyAction('ArrowDown', 0, enabled), {
		kind: 'focus',
		index: 2
	});
	assert.deepEqual(keys.quickDrawMenuKeyAction('ArrowDown', 3, enabled), {
		kind: 'focus',
		index: 0
	});
	assert.deepEqual(keys.quickDrawMenuKeyAction('ArrowUp', 0, enabled), { kind: 'focus', index: 3 });
	assert.deepEqual(keys.quickDrawMenuKeyAction('ArrowUp', 2, enabled), { kind: 'focus', index: 0 });
	assert.deepEqual(keys.quickDrawMenuKeyAction('ArrowDown', -1, enabled), {
		kind: 'focus',
		index: 0
	});
	assert.deepEqual(keys.quickDrawMenuKeyAction('ArrowUp', -1, enabled), {
		kind: 'focus',
		index: 3
	});
	assert.equal(keys.quickDrawMenuKeyAction('ArrowDown', 0, [false, false]), null);
});

// [if] Home or End is pressed [then] focus moves to the first or last enabled item, [else stop].
test('KEYB-14648: Home and End jump to the first and last enabled items', () => {
	const enabled = [false, true, true, false];
	assert.deepEqual(keys.quickDrawMenuKeyAction('Home', 2, enabled), { kind: 'focus', index: 1 });
	assert.deepEqual(keys.quickDrawMenuKeyAction('End', 1, enabled), { kind: 'focus', index: 2 });
});

// [if] Enter or Space is pressed on an enabled item [then] it activates as a plain click, disabled items do nothing, [else stop].
test('KEYB-14648: Enter and Space activate the focused enabled item only', () => {
	assert.deepEqual(keys.quickDrawMenuKeyAction('Enter', 1, [true, true]), { kind: 'activate' });
	assert.deepEqual(keys.quickDrawMenuKeyAction(' ', 0, [true, true]), { kind: 'activate' });
	assert.equal(keys.quickDrawMenuKeyAction('Enter', 1, [true, false]), null);
	assert.equal(keys.quickDrawMenuKeyAction('Enter', -1, [true]), null);
	assert.equal(keys.quickDrawMenuKeyAction('a', 0, [true]), null);
});

// [if] Escape or Tab is pressed in the menu [then] the menu closes so focus can return to its opener, [else stop].
test('KEYB-14648: Escape and Tab close the menu', () => {
	assert.deepEqual(keys.quickDrawMenuKeyAction('Escape', 0, [true]), { kind: 'close' });
	assert.deepEqual(keys.quickDrawMenuKeyAction('Escape', -1, [true]), { kind: 'close' });
	assert.deepEqual(keys.quickDrawMenuKeyAction('Tab', 0, [true]), { kind: 'close' });
});

// [if] a menu key lands on a focused menu item [then] /performance hotkeys leave it to the menu, other keys stay global, [else stop].
test('KEYB-14648: page hotkeys yield menu keys on a focused menu item', () => {
	for (const role of ['menuitem', 'menuitemcheckbox', 'menuitemradio']) {
		for (const key of ['ArrowUp', 'ArrowDown', 'Home', 'End', 'Enter', ' ', 'Escape', 'Tab']) {
			assert.equal(target.isMenuItemKeyTarget(menuItem(role), key), true, `${role} ${key}`);
		}
	}
	assert.equal(target.isMenuItemKeyTarget(menuItem('menuitem'), 'c'), false);
	assert.equal(target.isMenuItemKeyTarget(menuItem('button'), 'ArrowDown'), false);
	assert.equal(target.isMenuItemKeyTarget(null, 'ArrowDown'), false);
});
