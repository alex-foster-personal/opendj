/**
 * Library-row drag -> deck drop, the WKWebView regression.
 *
 * In the packaged app (WKWebView) dropping a library row on a deck did nothing
 * while double-click and right-click load both worked. Cause: the dragover
 * handlers gated acceptance on `dataTransfer.types.includes(custom mime)`.
 * WebKit does not expose custom MIME types in `types` during dragover, so
 * preventDefault() never ran, the deck never became a valid drop target, and
 * ondrop never fired at all - which also made the state-based fallback inside
 * the drop handler unreachable.
 *
 * Acceptance is now the in-app drag state (trackDrag.active). The custom MIME
 * is still SET on dragstart for cross-app interop, and is still read FIRST on
 * drop; the state is only the fallback.
 *
 * Regression lines:
 * - if acceptTrackDragOver stops calling preventDefault for a DragEvent with an
 *   EMPTY dataTransfer.types then deck drop is dead in WebKit again
 * - if acceptTrackDragOver accepts while no in-app drag is running then any
 *   foreign drag (a file, a text selection) starts loading decks
 * - if droppedStableIds stops preferring dataTransfer over the state then a
 *   multi-select drag loses its explicit payload
 * - if droppedStableIds stops falling back to the state then WebKit's protected
 *   drag mode (getData returns '') silently drops the load
 * - if dragstart stops installing a purpose-made drag image then WebKit
 *   snapshots the row plus every overlapping composited layer, which is the
 *   "quite a few UI components come along" ghost
 * - if a ghost survives dragend, or two ghosts can coexist, then offscreen
 *   nodes accumulate on the body for the life of the session
 * - if Deck.svelte or PlaylistTree.svelte grows a dataTransfer.types gate again
 *   then the WebKit bug is back on that surface
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SRC = fileURLToPath(new URL('../../src', import.meta.url));

function source(relative) {
	return readFileSync(`${SRC}/${relative}`, 'utf8');
}

/** Comment prose explains the WebKit rule and names the banned expression, so
 * an anti-pattern assertion has to read the code only. */
function code(relative) {
	return source(relative)
		.replace(/\/\*[\s\S]*?\*\//g, '')
		.split('\n')
		.filter((line) => !line.trimStart().startsWith('//'))
		.join('\n');
}

// ------------------------------------------------------------- DOM stubs
// Only the DOM is stubbed; the modules under test are the real ones.

class FakeElement {
	constructor(tagName) {
		this.tagName = tagName.toUpperCase();
		this.children = [];
		this.parentNode = null;
		this.style = { cssText: '' };
		this.className = '';
		this.textContent = '';
		this.attributes = {};
	}
	appendChild(child) {
		child.parentNode = this;
		this.children.push(child);
		return child;
	}
	remove() {
		if (this.parentNode === null) return;
		this.parentNode.children = this.parentNode.children.filter((c) => c !== this);
		this.parentNode = null;
	}
	setAttribute(name, value) {
		this.attributes[name] = value;
	}
	/** Depth-first text of the whole subtree, for asserting what the ghost says. */
	get allText() {
		if (this.children.length === 0) return this.textContent;
		return this.children.map((c) => c.allText).join('|');
	}
}

/** dataTransfer stand-in. `types` defaults to EMPTY - the WebKit shape. */
function fakeDataTransfer({ types = [], data = {} } = {}) {
	const calls = { setDragImage: [], setData: [] };
	return {
		types,
		dropEffect: 'none',
		effectAllowed: 'none',
		getData: (mime) => data[mime] ?? '',
		setData: (mime, value) => calls.setData.push([mime, value]),
		setDragImage: (...args) => calls.setDragImage.push(args),
		calls
	};
}

function fakeDragEvent(dataTransfer) {
	let prevented = 0;
	return {
		dataTransfer,
		preventDefault: () => {
			prevented += 1;
		},
		get preventedCount() {
			return prevented;
		}
	};
}

/** Fake `window`, capturing every listener registered on it by type so a test
 * can fire one the way a real abandoned drag does: the browser dispatches
 * `dragend` on the source element, it bubbles to `window`, and nothing here
 * ever calls `endTrackDrag()` directly. */
function installFakeWindow() {
	const listeners = new Map();
	globalThis.window = {
		addEventListener: (type, fn) => {
			const list = listeners.get(type) ?? [];
			list.push(fn);
			listeners.set(type, list);
		},
		removeEventListener: () => {}
	};
	return {
		fire: (type, event = {}) => {
			for (const fn of listeners.get(type) ?? []) fn(event);
		}
	};
}

let drag;
let ghostMod;
let body;
let fakeWindow;

before(async () => {
	body = new FakeElement('body');
	globalThis.document = {
		body,
		createElement: (tagName) => new FakeElement(tagName)
	};
	fakeWindow = installFakeWindow();
	drag = await loadTypeScriptModule('src/lib/rb/track-drag.svelte.ts');
	ghostMod = await loadTypeScriptModule('src/lib/rb/drag-ghost.ts');
});

beforeEach(() => {
	drag.endTrackDrag();
	ghostMod.removeTrackDragGhost();
	body.children = [];
});

// ------------------------------------------------------ dragover acceptance

test('acceptTrackDragOver: an in-app drag is accepted with an EMPTY types list', () => {
	drag.beginTrackDrag(['ID-1']);
	const dt = fakeDataTransfer({ types: [] });
	const event = fakeDragEvent(dt);
	assert.equal(drag.acceptTrackDragOver(event), true);
	assert.equal(event.preventedCount, 1, 'preventDefault is what makes the slot droppable');
	assert.equal(dt.dropEffect, 'copy');
});

test('acceptTrackDragOver: no in-app drag means no acceptance', () => {
	const dt = fakeDataTransfer({ types: [drag.TRACK_STABLE_MIME] });
	const event = fakeDragEvent(dt);
	assert.equal(drag.acceptTrackDragOver(event), false);
	assert.equal(event.preventedCount, 0, 'a foreign drag must stay unhandled');
	assert.equal(dt.dropEffect, 'none');
});

test('acceptTrackDragOver: a null dataTransfer still accepts, and does not throw', () => {
	drag.beginTrackDrag(['ID-1']);
	const event = fakeDragEvent(null);
	assert.equal(drag.acceptTrackDragOver(event), true);
	assert.equal(event.preventedCount, 1);
});

// ---------------------------------------------------------- drop payload

test('droppedStableIds: dataTransfer wins when the browser hands it over', () => {
	drag.beginTrackDrag(['STATE-1']);
	const event = fakeDragEvent(
		fakeDataTransfer({ data: { [drag.TRACK_STABLE_MIME]: ' DT-1 , DT-2 ' } })
	);
	assert.deepEqual(drag.droppedStableIds(event), ['DT-1', 'DT-2']);
	assert.equal(drag.primaryDroppedStableId(event), 'DT-1');
});

test('droppedStableIds: falls back to the in-app state when getData returns empty', () => {
	drag.beginTrackDrag(['STATE-1', 'STATE-2']);
	const event = fakeDragEvent(fakeDataTransfer());
	assert.deepEqual(drag.droppedStableIds(event), ['STATE-1', 'STATE-2']);
	assert.equal(drag.primaryDroppedStableId(event), 'STATE-1');
});

test('droppedStableIds: no transfer and no state is empty, not a phantom load', () => {
	const event = fakeDragEvent(fakeDataTransfer());
	assert.deepEqual(drag.droppedStableIds(event), []);
	assert.equal(drag.primaryDroppedStableId(event), null);
});

test('endTrackDrag clears the fallback so a finished drag cannot load a deck', () => {
	drag.beginTrackDrag(['STATE-1']);
	drag.endTrackDrag();
	assert.equal(drag.trackDrag.active, false);
	assert.equal(drag.primaryDroppedStableId(fakeDragEvent(fakeDataTransfer())), null);
});

test('beginTrackDrag stores row flags for droppedRowFlags', () => {
	const flags = { file_exists: false, is_streaming: false };
	drag.beginTrackDrag(['ID-1'], { 'ID-1': flags });
	assert.deepEqual(drag.droppedRowFlags('ID-1'), flags);
});

test('endTrackDrag clears row flags', () => {
	drag.beginTrackDrag(['ID-1'], { 'ID-1': { file_exists: false, is_streaming: false } });
	drag.endTrackDrag();
	assert.equal(drag.droppedRowFlags('ID-1'), null);
});

test('single-arg beginTrackDrag leaves droppedRowFlags null', () => {
	drag.beginTrackDrag(['ID-1']);
	assert.equal(drag.droppedRowFlags('ID-1'), null);
});

// -------------------------------------------------- abandoned drag (DECKUX-03)

test('a drag abandoned with no drop is cleared by the window-level dragend listener', () => {
	// No drop ever happens here: only dragstart, then the browser's own
	// dragend when the drag is released outside any drop target. Nothing in
	// this test calls endTrackDrag() itself - firing the LISTENER is the
	// point, so a broken or unregistered listener fails this for the right
	// reason instead of passing on the direct call above.
	drag.beginTrackDrag(['ABANDONED-1'], {
		'ABANDONED-1': { file_exists: true, is_streaming: false }
	});
	assert.equal(drag.trackDrag.active, true, 'setup: the drag must be armed before it is abandoned');

	fakeWindow.fire('dragend');

	assert.equal(drag.trackDrag.active, false, 'a stale armed state blocks every later drag');
	assert.deepEqual(drag.trackDrag.stableIds, []);
	assert.equal(drag.droppedRowFlags('ABANDONED-1'), null);
	assert.equal(drag.primaryDroppedStableId(fakeDragEvent(fakeDataTransfer())), null);
});

test('after an abandoned drag is cleared, a fresh drag onto a deck is not blocked', () => {
	drag.beginTrackDrag(['ABANDONED-2']);
	fakeWindow.fire('dragend');

	// The next drag must behave exactly as a first drag would: acceptance
	// still comes from the in-app state, and it must be the NEW ids, not a
	// stale leftover from the abandoned one.
	drag.beginTrackDrag(['FRESH-1']);
	const event = fakeDragEvent(fakeDataTransfer());
	assert.equal(drag.acceptTrackDragOver(event), true);
	assert.deepEqual(drag.droppedStableIds(event), ['FRESH-1']);
});

// --------------------------------------------------------------- drag ghost

test('installTrackDragGhost: one offscreen element, handed to setDragImage', () => {
	const dt = fakeDataTransfer();
	const event = fakeDragEvent(dt);
	ghostMod.installTrackDragGhost(event, { title: 'Lanterns', artist: 'Marlow Quay', count: 1 });

	assert.equal(body.children.length, 1, 'exactly one ghost node on the body');
	const [ghost] = body.children;
	assert.match(ghost.style.cssText, /position:\s*fixed/);
	assert.match(ghost.style.cssText, /top:\s*-10000px/);
	assert.equal(dt.calls.setDragImage.length, 1, 'WebKit snapshots the row without this call');
	const [element, offsetX, offsetY] = dt.calls.setDragImage[0];
	assert.equal(element, ghost, 'the snapshot target must be the purpose-made ghost');
	assert.ok(offsetX > 0 && offsetX < 40, `offset x ${offsetX} should stay near the cursor`);
	assert.ok(offsetY > 0 && offsetY < 40, `offset y ${offsetY} should stay near the cursor`);
	assert.match(ghost.allText, /Lanterns/);
	assert.match(ghost.allText, /Marlow Quay/);
});

test('installTrackDragGhost: a single track carries no count badge', () => {
	const event = fakeDragEvent(fakeDataTransfer());
	ghostMod.installTrackDragGhost(event, { title: 'Lanterns', artist: 'Marlow Quay', count: 1 });
	assert.equal(/\btracks?\b/.test(body.children[0].allText), false);
});

test('installTrackDragGhost: a multi-select drag says how many tracks', () => {
	const event = fakeDragEvent(fakeDataTransfer());
	ghostMod.installTrackDragGhost(event, { title: 'Lanterns', artist: 'Marlow Quay', count: 7 });
	assert.match(body.children[0].allText, /7 tracks/);
});

test('removeTrackDragGhost: dragend leaves nothing behind', () => {
	ghostMod.installTrackDragGhost(fakeDragEvent(fakeDataTransfer()), {
		title: 'A',
		artist: 'B',
		count: 1
	});
	ghostMod.removeTrackDragGhost();
	assert.equal(body.children.length, 0);
	ghostMod.removeTrackDragGhost();
	assert.equal(body.children.length, 0, 'a second removal must be a no-op, not a throw');
});

test('installTrackDragGhost: a second dragstart replaces rather than stacks', () => {
	const spec = { title: 'A', artist: 'B', count: 1 };
	ghostMod.installTrackDragGhost(fakeDragEvent(fakeDataTransfer()), spec);
	ghostMod.installTrackDragGhost(fakeDragEvent(fakeDataTransfer()), spec);
	assert.equal(body.children.length, 1, 'a missed dragend must not accumulate ghosts');
});

test('installTrackDragGhost: no dataTransfer means no orphan node on the body', () => {
	ghostMod.installTrackDragGhost(fakeDragEvent(null), { title: 'A', artist: 'B', count: 1 });
	assert.equal(body.children.length, 0);
});

// ------------------------------------------------------------- wiring facts
// Source-shape only where the harness cannot mount a component (the
// inert-controls.test.mjs / jobs-drawer.test.mjs precedent).

test('every track-drop surface accepts on drag state, never on dataTransfer.types', () => {
	for (const file of [
		'lib/components/rb/Deck.svelte',
		'lib/components/rb/browser/PlaylistTree.svelte'
	]) {
		const text = code(file);
		assert.match(text, /acceptTrackDragOver\(/, `${file} must share the acceptance helper`);
		assert.equal(
			/dataTransfer\s*\??\.\s*types/.test(text),
			false,
			`${file} sniffs dataTransfer.types - WebKit hides custom MIME types during dragover`
		);
	}
});

test('exactly one component owns track drops on a deck', () => {
	// DeckLoadDropOverlay.svelte was a second, never-mounted copy of this logic.
	// Two divergent copies is how the live one went unfixed.
	const page = source('routes/performance/+page.svelte');
	assert.equal(page.includes('DeckLoadDropOverlay'), false);
	assert.match(source('lib/components/rb/Deck.svelte'), /ondrop=/);
});

test('the row drag still SETS the custom MIME for cross-app interop', () => {
	const table = source('lib/components/rb/browser/TrackTable.svelte');
	assert.match(table, /setData\(TRACK_STABLE_MIME,/);
	assert.equal(
		/const MIME_TRACK =/.test(table),
		false,
		'the MIME literal must come from track-drag, not a per-component copy'
	);
});

test('dragstart installs the ghost and dragend removes it', () => {
	const table = source('lib/components/rb/browser/TrackTable.svelte');
	assert.match(table, /installTrackDragGhost\(/);
	assert.match(table, /removeTrackDragGhost\(\)/);
});
