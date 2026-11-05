// requirement: UX-FLOAT-01
/**
 * triggerFloatingAction measures its trigger with the floating node already
 * out of flow.
 *
 * Regression (React deck part B, story parity): LibraryEditMenu's list sits
 * in flow beside the pencil until its first placement makes it fixed. Placed
 * from a trigger rect read while the list was still in flow (the open list
 * stretched the inline-flex pencil to 67px tall), the menu landed 52px below
 * the pencil, and nothing re-placed it once the list left flow and the pencil
 * shrank back: about one Storybook load in four.
 */
import assert from 'node:assert/strict';
import { afterEach, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const TRIGGER_TOP = 16;
const TRIGGER_HEIGHT = 15;
// The pencil's height while the open list is still in flow beside it.
const STRETCHED_HEIGHT = 67;

let clamp;
let saved;

before(async () => {
	clamp = await loadTypeScriptModule('src/lib/ui/clamp-to-viewport.ts');
});

beforeEach(() => {
	saved = {
		window: globalThis.window,
		ResizeObserver: globalThis.ResizeObserver,
		requestAnimationFrame: globalThis.requestAnimationFrame
	};
	globalThis.window = {
		innerWidth: 800,
		innerHeight: 400,
		addEventListener() {},
		removeEventListener() {}
	};
	globalThis.ResizeObserver = class {
		observe() {}
		disconnect() {}
	};
	globalThis.requestAnimationFrame = () => 0;
});

afterEach(() => {
	globalThis.window = saved.window;
	globalThis.ResizeObserver = saved.ResizeObserver;
	globalThis.requestAnimationFrame = saved.requestAnimationFrame;
});

function setup() {
	const node = { offsetWidth: 101, offsetHeight: 51, style: { position: '', left: '', top: '' } };
	const trigger = {
		getBoundingClientRect: () => ({
			left: 16,
			top: TRIGGER_TOP,
			width: 23,
			height: node.style.position === 'fixed' ? TRIGGER_HEIGHT : STRETCHED_HEIGHT
		})
	};
	return { node, trigger };
}

test('the first placement reads the trigger with the node already fixed', () => {
	const { node, trigger } = setup();
	const action = clamp.triggerFloatingAction(node, {
		getTrigger: () => trigger,
		preferred: 'below',
		gap: 4
	});
	action.update({ getTrigger: () => trigger, preferred: 'below', gap: 4 });
	assert.equal(node.style.position, 'fixed');
	assert.equal(node.style.top, `${TRIGGER_TOP + TRIGGER_HEIGHT + 4}px`);
	action.destroy();
});

test('control: a trigger that really is tall still places the node below all of it', () => {
	const { node } = setup();
	const tall = {
		getBoundingClientRect: () => ({
			left: 16,
			top: TRIGGER_TOP,
			width: 23,
			height: STRETCHED_HEIGHT
		})
	};
	const action = clamp.triggerFloatingAction(node, {
		getTrigger: () => tall,
		preferred: 'below',
		gap: 4
	});
	action.update({ getTrigger: () => tall, preferred: 'below', gap: 4 });
	assert.equal(node.style.top, `${TRIGGER_TOP + STRETCHED_HEIGHT + 4}px`);
	action.destroy();
});
