// requirement: MIXUX-08
// requirement: PERF-UI-13
// [if] horizontal wheel dominates [then] routing helper accepts it and respects library guard
// [if] no mixer deck selected [then] install binds no wheel until a deck is chosen, else stop.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { afterEach, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let wheel;
let routing;
let m;

const savedGlobal = {
	window: globalThis.window,
	requestAnimationFrame: globalThis.requestAnimationFrame,
	cancelAnimationFrame: globalThis.cancelAnimationFrame,
	Element: globalThis.Element
};

before(async () => {
	wheel = await loadTypeScriptModule('src/lib/rb/wheel-adjust.ts');
	routing = await loadTypeScriptModule('src/lib/rb/mixer-horizontal-wheel.ts');
	m = await loadTypeScriptModule('tests/unit/fixtures/mixer-performance-wheel-entry.ts');
});

afterEach(() => {
	globalThis.window = savedGlobal.window;
	globalThis.requestAnimationFrame = savedGlobal.requestAnimationFrame;
	globalThis.cancelAnimationFrame = savedGlobal.cancelAnimationFrame;
	globalThis.Element = savedGlobal.Element;
});

function fakeWindow() {
	const calls = [];
	const listeners = new Map();
	if (globalThis.Element === undefined) {
		globalThis.Element = class Element {};
	}
	globalThis.window = {
		addEventListener(type, fn, opts) {
			calls.push(['add', type, opts]);
			listeners.set(type, fn);
		},
		removeEventListener(type, fn, _opts) {
			calls.push(['remove', type, _opts]);
			if (listeners.get(type) === fn) listeners.delete(type);
		}
	};
	globalThis.requestAnimationFrame = () => 1;
	globalThis.cancelAnimationFrame = () => {};
	const wheelCalls = (kind) => calls.filter((c) => c[0] === kind && c[1] === 'wheel');
	return { calls, listeners, wheelCalls };
}

function wheelEvent(deltaX, deltaY = 0) {
	const event = new Event('wheel', { cancelable: true });
	event.deltaX = deltaX;
	event.deltaY = deltaY;
	return event;
}

test('horizontalWheelDirection prefers deltaX', () => {
	assert.equal(wheel.horizontalWheelDirection(wheelEvent(10, 0)), -1);
	assert.equal(wheel.horizontalWheelDirection(wheelEvent(-8, 0)), 1);
	assert.equal(wheel.horizontalWheelDirection(wheelEvent(2, 9)), 0);
});

test('library subtree blocks horizontal wheel routing', async () => {
	const src = await readFile('src/lib/rb/mixer-horizontal-wheel.ts', 'utf8');
	assert.match(src, /closest\('\[data-library-root\]'\)/);
});

test('filter and color prefs both route to filter command for now', () => {
	assert.equal(routing.knobCommandType('filter'), 'filter');
	assert.equal(routing.knobCommandType('color'), 'filter');
});

test('PERF-UI-13: no deck selected binds no window wheel; selecting binds one non-passive; clearing removes it', () => {
	m.clearSelection();
	const { calls, listeners, wheelCalls } = fakeWindow();
	const uninstall = m.installMixerPerformanceListeners();
	assert.ok(
		calls.some((c) => c[0] === 'add' && c[1] === 'keydown'),
		'control: keydown bound on fake window'
	);
	assert.ok(
		calls.some((c) => c[0] === 'add' && c[1] === 'pointerdown'),
		'control: pointerdown bound on fake window'
	);
	assert.deepEqual(wheelCalls('add'), []);
	assert.equal(listeners.has('wheel'), false);

	m.clickSelect(2, false);
	assert.equal(wheelCalls('add').length, 1);
	assert.deepEqual(wheelCalls('add')[0][2], { passive: false, capture: true });

	m.clickSelect(3, true);
	assert.equal(wheelCalls('add').length, 1);

	m.clearSelection();
	assert.equal(wheelCalls('remove').length, 1);
	assert.equal(listeners.has('wheel'), false);
	const removeAfterFirstClear = wheelCalls('remove').length;
	m.clearSelection();
	assert.equal(wheelCalls('remove').length, removeAfterFirstClear);

	m.resetToSingle(1);
	assert.equal(wheelCalls('add').length, 2);

	uninstall();
	assert.equal(wheelCalls('remove').length, 2);
	m.clickSelect(4, false);
	assert.equal(wheelCalls('add').length, 2);
});

test('PERF-UI-13: installing with a deck already selected binds the wheel at once, and teardown removes it', () => {
	m.resetToSingle(1);
	const { listeners, wheelCalls } = fakeWindow();
	const uninstall = m.installMixerPerformanceListeners();
	assert.equal(wheelCalls('add').length, 1);
	assert.deepEqual(wheelCalls('add')[0][2], { passive: false, capture: true });
	uninstall();
	assert.equal(wheelCalls('remove').length, 1);
	assert.equal(listeners.has('wheel'), false);
});

test('MIXUX-08: with a deck selected the bound wheel handler preventDefaults a horizontal wheel', () => {
	m.resetToSingle(1);
	const { listeners } = fakeWindow();
	const uninstall = m.installMixerPerformanceListeners();
	const onWheel = listeners.get('wheel');
	assert.equal(typeof onWheel, 'function');
	const horizontal = wheelEvent(10, 0);
	onWheel(horizontal);
	assert.equal(horizontal.defaultPrevented, true);
	const vertical = wheelEvent(0, 10);
	onWheel(vertical);
	assert.equal(vertical.defaultPrevented, false);
	uninstall();
});
