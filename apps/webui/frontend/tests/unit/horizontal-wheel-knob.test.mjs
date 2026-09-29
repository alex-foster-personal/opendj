// requirement: MIXUX-08
// [if] horizontal wheel dominates [then] routing helper accepts it and respects library guard
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let wheel;
let routing;

before(async () => {
	wheel = await loadTypeScriptModule('src/lib/rb/wheel-adjust.ts');
	routing = await loadTypeScriptModule('src/lib/rb/mixer-horizontal-wheel.ts');
});

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
