// requirement: UX-FLOAT-01
// requirement: LIBM-29
/**
 * pointFloatingAction keeps a pointer-anchored fixed menu inside the viewport
 * for EVERY size it takes, not only the size it had when it opened.
 *
 * Regression (PR #3896 e2e, track-playlists.spec.ts): Show in playlists
 * opened at the pointer (808, 673.75) in a 1280x720 window, clamped once
 * against its one-row "Loading playlists..." box, then grew to five items.
 * The third item landed at y~736, outside the viewport, where Playwright (and
 * a user) cannot click it: a fixed node cannot be scrolled into view.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const FRONTEND_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const VIEWPORT = { width: 1280, height: 720 };
const MARGIN = 8;
// Measured in the failing trace: the right-click point on the first track row.
const POINTER = { x: 808, y: 673.75 };
const LOADING_SIZE = { width: 190, height: 34 };
const LOADED_SIZE = { width: 220, height: 138 };

let clamp;
let observers;
let windowListeners;
let savedWindow;
let savedResizeObserver;

before(async () => {
	clamp = await loadTypeScriptModule('src/lib/ui/clamp-to-viewport.ts');
});

beforeEach(() => {
	observers = [];
	windowListeners = new Map();
	savedWindow = globalThis.window;
	savedResizeObserver = globalThis.ResizeObserver;
	globalThis.window = {
		innerWidth: VIEWPORT.width,
		innerHeight: VIEWPORT.height,
		addEventListener(type, fn) {
			windowListeners.set(type, fn);
		},
		removeEventListener(type, fn) {
			if (windowListeners.get(type) === fn) windowListeners.delete(type);
		}
	};
	globalThis.ResizeObserver = class {
		constructor(callback) {
			this.callback = callback;
			this.targets = [];
			this.disconnected = false;
			observers.push(this);
		}
		observe(target) {
			this.targets.push(target);
		}
		disconnect() {
			this.disconnected = true;
			this.targets = [];
		}
	};
});

afterEach(() => {
	globalThis.window = savedWindow;
	globalThis.ResizeObserver = savedResizeObserver;
});

function makeNode(size) {
	return { offsetWidth: size.width, offsetHeight: size.height, style: {} };
}

/** What the browser does when the node's content changes its box. */
function resize(node, size) {
	node.offsetWidth = size.width;
	node.offsetHeight = size.height;
	for (const observer of observers) {
		if (observer.targets.includes(node)) observer.callback([]);
	}
}

function box(node) {
	return {
		left: Number.parseFloat(node.style.left),
		top: Number.parseFloat(node.style.top),
		bottom: Number.parseFloat(node.style.top) + node.offsetHeight,
		right: Number.parseFloat(node.style.left) + node.offsetWidth
	};
}

test('a menu that grows after opening near the bottom is pulled back inside the viewport', () => {
	const node = makeNode(LOADING_SIZE);
	clamp.pointFloatingAction(node, POINTER);
	// The one-row loading box fits at the pointer, so it opens exactly there.
	assert.equal(box(node).top, Math.round(POINTER.y));

	resize(node, LOADED_SIZE);

	const placed = box(node);
	assert.ok(
		placed.bottom <= VIEWPORT.height - MARGIN,
		`grown menu bottom ${placed.bottom} is past the viewport inset ${VIEWPORT.height - MARGIN}`
	);
	assert.equal(placed.top, VIEWPORT.height - LOADED_SIZE.height - MARGIN);
	assert.equal(placed.left, POINTER.x);
});

test('a menu that shrinks back returns to the requested point instead of staying pushed up', () => {
	const node = makeNode(LOADED_SIZE);
	clamp.pointFloatingAction(node, POINTER);
	assert.equal(box(node).top, VIEWPORT.height - LOADED_SIZE.height - MARGIN);

	resize(node, LOADING_SIZE);

	assert.equal(box(node).top, Math.round(POINTER.y));
});

test('a menu opened with room to spare stays at the pointer when it grows', () => {
	const node = makeNode(LOADING_SIZE);
	clamp.pointFloatingAction(node, { x: 300, y: 120 });
	resize(node, LOADED_SIZE);
	assert.deepEqual(
		{ left: box(node).left, top: box(node).top },
		{ left: 300, top: 120 }
	);
});

test('a window resize re-clamps, and destroy stops both observers', () => {
	const node = makeNode(LOADED_SIZE);
	const action = clamp.pointFloatingAction(node, { x: 300, y: 500 });
	assert.equal(box(node).top, 500);

	globalThis.window.innerHeight = 560;
	windowListeners.get('resize')();
	assert.equal(box(node).top, 560 - LOADED_SIZE.height - MARGIN);

	action.destroy();
	assert.equal(windowListeners.has('resize'), false);
	assert.ok(observers.every((observer) => observer.disconnected));
});

test('update moves the anchor to a new pointer', () => {
	const node = makeNode(LOADING_SIZE);
	const action = clamp.pointFloatingAction(node, { x: 300, y: 120 });
	action.update({ x: 400, y: 200 });
	assert.deepEqual({ left: box(node).left, top: box(node).top }, { left: 400, top: 200 });
});

for (const file of ['TrackPlaylistsPopover.svelte', 'RelocatePopover.svelte']) {
	test(`${file} places itself with pointFloatingAction, not a one-shot clamp`, () => {
		const source = fs.readFileSync(
			path.join(FRONTEND_ROOT, 'src/lib/components/rb/browser', file),
			'utf8'
		);
		assert.match(source, /use:pointFloatingAction=\{\{ x, y \}\}/);
		assert.doesNotMatch(source, /clampToViewport\(/);
	});
}
