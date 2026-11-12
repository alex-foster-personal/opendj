/**
 * @pytest.mark.requirement LIBM-12633
 * Pins 535c07d9fe88 / ce142ae7e22f: preference-backed library list scroll.
 *
 * [if] one mouse-wheel detent scales down [then] fewer than native rows, [else stop].
 * [if] factor is 1 [then] native scroll, [else stop].
 * [if] library scroll sensitivity missing from settings [then] stop.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

const read = (p) => readFileSync(fileURLToPath(new URL(`../../${p}`, import.meta.url)), 'utf8');
const wheel = (o = {}) => ({ deltaX: 0, deltaY: 0, deltaMode: 0, ctrlKey: false, ...o });
const MOUSE_NOTCH_PX = 120;
const SHORTEST_ROW_PX = 22;

let mod;
before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/library-wheel-scroll.ts');
});

test('defaults shorten wheel distance below native', () => {
	assert.ok(mod.LIBRARY_LIST_WHEEL_DEFAULTS.mouse < 1);
	assert.ok(mod.LIBRARY_LIST_WHEEL_DEFAULTS.trackpad < 1);
});

test('one mouse notch moves the list no more than a few rows', () => {
	const delta = mod.libraryWheelScrollDelta(
		wheel({ deltaY: MOUSE_NOTCH_PX }),
		mod.LIBRARY_LIST_WHEEL_DEFAULTS.mouse
	);
	assert.notEqual(delta, null);
	assert.ok(delta.top < MOUSE_NOTCH_PX);
	assert.ok(delta.top / SHORTEST_ROW_PX <= 3);
});

test('horizontal distance is not scaled', () => {
	const delta = mod.libraryWheelScrollDelta(
		wheel({ deltaX: 30, deltaY: 10 }),
		mod.LIBRARY_LIST_WHEEL_DEFAULTS.mouse
	);
	assert.equal(delta.left, 30);
});

test('pinch-zoom and line or page mode stay native', () => {
	assert.equal(mod.libraryWheelScrollDelta(wheel({ deltaY: 50, ctrlKey: true }), 0.5), null);
	assert.equal(mod.libraryWheelScrollDelta(wheel({ deltaY: 3, deltaMode: 1 }), 0.5), null);
});

test('factor 1 intercepts nothing', () => {
	assert.equal(mod.libraryWheelScrollDelta(wheel({ deltaY: 50 }), 1), null);
});

test('settings catalog exposes library scroll mouse and trackpad rows', () => {
	const catalog = read('src/lib/settings/catalog.ts');
	assert.match(catalog, /id: 'wheel_sensitivity\.library_mouse'/);
	assert.match(catalog, /id: 'wheel_sensitivity\.library_trackpad'/);
});

test('the action scrolls when factor is below 1 and stops at factor 1', () => {
	const listeners = {};
	const node = {
		scrollTop: 200,
		scrollLeft: 0,
		addEventListener: (type, fn, opts) => {
			listeners[type] = { fn, opts };
		},
		removeEventListener: (type) => {
			delete listeners[type];
		}
	};
	const handle = mod.libraryWheelScroll(node);
	assert.equal(listeners.wheel.opts.passive, false);
	let prevented = 0;
	listeners.wheel.fn({
		...wheel({ deltaY: MOUSE_NOTCH_PX, wheelDeltaY: MOUSE_NOTCH_PX }),
		preventDefault: () => prevented++
	});
	assert.equal(prevented, 1);
	const factor = mod.LIBRARY_LIST_WHEEL_DEFAULTS.mouse;
	assert.equal(node.scrollTop, 200 + MOUSE_NOTCH_PX * factor);
	mod.setLibraryListWheelSensitivity('mouse', 1);
	mod.setLibraryListWheelSensitivity('trackpad', 1);
	assert.equal(listeners.wheel, undefined, 'factor 1 on both inputs removes the listener');
	handle.destroy();
	mod.resetLibraryListWheelSensitivity();
});
