/**
 * Source-shape for the Load-to-CHn click-drag intro blend (issue #286).
 *
 * The menu wires pointer capture and real dispatcher commands; there is no
 * node:test-able seam that can drag a live Load item, so this file reads the
 * sources the same way quick-draw-vocal-fix.test.mjs does.
 *
 * Regression lines:
 * - if Load onclick still calls item.run in addition to pointerdown, a click double-loads
 * - if leave-dismiss closes mid-capture, the morph sticks EQ with no pointerup
 * - if click path writes intro EQ, a tap leaves Low at 0.3
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

function read(rel) {
	return readFileSync(fileURLToPath(new URL(`../../${rel}`, import.meta.url)), 'utf8');
}

const menu = read('src/lib/components/rb/QuickDrawMenu.svelte');
const hud = read('src/lib/components/rb/LoadBlendHud.svelte');
const dispatch = read('src/lib/rb/load-blend-dispatch.ts');
const gesture = read('src/lib/rb/load-blend-gesture.ts');

test('Load items carry data-testid quick-draw-load-ch', () => {
	assert.match(menu, /testId: `quick-draw-load-ch\$\{deck\}`/);
	assert.match(menu, /data-testid=\{item\.testId\}/);
});

test('menu uses setPointerCapture on Load pointerdown', () => {
	assert.match(menu, /setPointerCapture\(e\.pointerId\)/);
	assert.match(menu, /onLoadPointerDown/);
	assert.match(menu, /onpointerdown=\{\(e\) => \{/);
});

test('onMenuPointerLeave / window pointerdown / Escape consult a gesture-active flag before _close', () => {
	assert.match(menu, /function onMenuPointerLeave[\s\S]*blend\.isActive\(\)/);
	assert.match(menu, /if \(blend\.isActive\(\)\) return;/);
	assert.match(menu, /if \(blend\.isActive\(\)\) blend\.abort\(\);/);
	assert.match(menu, /function _close\(\)[\s\S]*blend\.isActive\(\)/);
});

test('dispatches go through runPerformanceCommandFromUi (no direct engine.setFader)', () => {
	assert.match(menu, /runPerformanceCommandFromUi/);
	assert.match(menu, /dispatchPerformanceCommand/);
	assert.doesNotMatch(menu, /engine\.setFader/);
	assert.doesNotMatch(dispatch, /engine\.setFader/);
	assert.doesNotMatch(dispatch, /engine\.setEq/);
});

test('HUD test id load-blend-hud exists in LoadBlendHud.svelte', () => {
	assert.match(hud, /data-testid="load-blend-hud"/);
	assert.match(menu, /LoadBlendHud/);
});

test('Load onclick does not also run item.run (click would double-load)', () => {
	assert.match(menu, /loadPressArmed/);
	assert.match(menu, /if \(loadPressArmed\) \{/);
	assert.match(menu, /blend\.down\(/);
	assert.match(menu, /item\.run\(\)/);
});

test('click path does not write intro EQ on pointerdown (a tap must not leave Low at 0.3)', () => {
	assert.match(gesture, /phase = 'morph'/);
	assert.match(dispatch, /result\.entered/);
	assert.match(dispatch, /LOAD_BLEND_INCOMING_START/);
	assert.doesNotMatch(
		dispatch,
		/pointerDown[\s\S]{0,400}LOAD_BLEND_INCOMING_START/
	);
});
