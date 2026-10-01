/**
 * @pytest.mark.requirement PVPIN-19
 * Pin 535c07d9fe88: scroll distance in the library track list was too high.
 *
 * [if] one mouse wheel notch is applied over the track list [then] the list
 *   moves no more than a few rows [else stop].
 * [if] the wheel is a pinch-zoom or a line/page-mode event [then] the handler
 *   leaves it to the browser [else stop].
 * [if] the factor is 1 [then] nothing is intercepted [else stop].
 *
 * Regression lines:
 * - if a 100 px notch still moves the list 100 px then nothing was fixed
 * - if direction is lost or horizontal scroll is scaled then the columns
 *   cannot be reached the way they could before -> broken
 * - if TrackTable stops applying the action then the helper is dead code
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const read = (p) => readFileSync(fileURLToPath(new URL(`../../${p}`, import.meta.url)), 'utf8');
const wheel = (o = {}) => ({ deltaX: 0, deltaY: 0, deltaMode: 0, ctrlKey: false, ...o });
/** Chromium and WKWebView report one notch of a notched wheel as 100-120 px. */
const MOUSE_NOTCH_PX = 120;
/** Compact library row height (TrackTable ROW_HEIGHT_COMPACT); cosy is 30 px. */
const SHORTEST_ROW_PX = 22;

let mod;
before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/library-wheel-scroll.ts');
});

test('the shipped factor shortens the distance', () => {
	assert.ok(mod.LIBRARY_WHEEL_DISTANCE_FACTOR > 0);
	assert.ok(mod.LIBRARY_WHEEL_DISTANCE_FACTOR < 1);
});

test('one mouse notch moves the list no more than a few rows', () => {
	const delta = mod.libraryWheelScrollDelta(wheel({ deltaY: MOUSE_NOTCH_PX }));
	assert.notEqual(delta, null);
	assert.ok(delta.top > 0, 'direction kept');
	assert.ok(delta.top < MOUSE_NOTCH_PX, 'shorter than the browser default');
	assert.ok(delta.top / SHORTEST_ROW_PX <= 3, `${delta.top / SHORTEST_ROW_PX} rows`);
});

test('scrolling up is scaled the same and keeps its sign', () => {
	const down = mod.libraryWheelScrollDelta(wheel({ deltaY: 40 }));
	const up = mod.libraryWheelScrollDelta(wheel({ deltaY: -40 }));
	assert.equal(up.top, -down.top);
});

test('horizontal distance is not scaled', () => {
	const delta = mod.libraryWheelScrollDelta(wheel({ deltaX: 30, deltaY: 10 }));
	assert.equal(delta.left, 30);
});

test('pinch-zoom, line mode and page mode are left to the browser', () => {
	assert.equal(mod.libraryWheelScrollDelta(wheel({ deltaY: 50, ctrlKey: true })), null);
	assert.equal(mod.libraryWheelScrollDelta(wheel({ deltaY: 3, deltaMode: 1 })), null);
	assert.equal(mod.libraryWheelScrollDelta(wheel({ deltaY: 1, deltaMode: 2 })), null);
});

test('a factor of 1 intercepts nothing; a bad factor throws', () => {
	assert.equal(mod.libraryWheelScrollDelta(wheel({ deltaY: 50 }), 1), null);
	for (const bad of [0, -1, Number.NaN, 1.5]) {
		assert.throws(() => mod.libraryWheelScrollDelta(wheel({ deltaY: 50 }), bad), /factor/);
	}
});

test('the action scrolls the node by the scaled distance and stops the default', () => {
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
	assert.equal(listeners.wheel.opts.passive, false, 'a passive listener cannot stop the default');
	let prevented = 0;
	listeners.wheel.fn({ ...wheel({ deltaY: MOUSE_NOTCH_PX }), preventDefault: () => prevented++ });
	assert.equal(prevented, 1);
	assert.equal(node.scrollTop, 200 + MOUSE_NOTCH_PX * mod.LIBRARY_WHEEL_DISTANCE_FACTOR);
	// Control: an event the helper declines is not prevented and moves nothing.
	listeners.wheel.fn({
		...wheel({ deltaY: 50, ctrlKey: true }),
		preventDefault: () => prevented++
	});
	assert.equal(prevented, 1);
	handle.destroy();
	assert.equal(listeners.wheel, undefined);
});

test('TrackTable applies the action to its scroll container', () => {
	const table = read('src/lib/components/rb/browser/TrackTable.svelte');
	const wrapAt = table.indexOf('class="table-wrap"');
	assert.ok(wrapAt > 0, 'table-wrap missing (control)');
	const tag = table.slice(wrapAt, table.indexOf('>\n', table.indexOf('onscroll=', wrapAt) + 200));
	assert.match(table.slice(wrapAt, wrapAt + 400), /use:libraryWheelScroll/);
	assert.ok(tag.length > 0);
});
