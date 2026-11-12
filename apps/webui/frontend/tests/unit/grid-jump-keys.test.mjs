import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * DECKUX-6794 keys ((private path) AC-10, US-5): Cmd/Ctrl+Left
 * and Right, mirrored by Cmd+A and Cmd+D (the app-wide WASD rule), jump the
 * selected deck on the fixed grid behind the `deck.snapped_jump` flag.
 *
 * Regression lines:
 * - if the keys bind with the flag off then broken
 * - if Cmd+A takes Select all away from the library without a deck press then broken
 * - if a text field loses its own Cmd+arrow line keys then broken
 * - if a held key fires a jump per repeat then broken
 */

let routing;

before(async () => {
	routing = await loadTypeScriptModule('src/lib/rb/performance-shortcut-routing.ts');
});

const PAGE = { tagName: 'DIV', getAttribute: () => null };
const TEXT = { tagName: 'INPUT', type: 'text', getAttribute: () => null };

function key(k, extra = {}) {
	return {
		code: '',
		key: k,
		target: PAGE,
		metaKey: true,
		ctrlKey: false,
		altKey: false,
		shiftKey: false,
		...extra
	};
}
const ON = { gridJump: true };
const resolve = (e, ctx = ON) => routing.resolvePerformanceShortcutAction(e, ctx);

test('flag off: Cmd+arrows stay unbound', () => {
	assert.equal(resolve(key('ArrowRight'), {}), null);
	assert.equal(resolve(key('ArrowLeft'), { gridJump: false }), null);
});

test('Cmd/Ctrl+Right and Left jump 4 bars, Shift 8', () => {
	assert.deepEqual(resolve(key('ArrowRight')), { kind: 'grid-jump', bars: 4, repeat: false });
	assert.deepEqual(resolve(key('ArrowLeft')), { kind: 'grid-jump', bars: -4, repeat: false });
	assert.deepEqual(resolve(key('ArrowRight', { shiftKey: true })), {
		kind: 'grid-jump',
		bars: 8,
		repeat: false
	});
	assert.deepEqual(resolve(key('ArrowLeft', { metaKey: false, ctrlKey: true })), {
		kind: 'grid-jump',
		bars: -4,
		repeat: false
	});
});

// if the library's instant double fires an 8-bar grid jump too then broken
test('library focus: Cmd+Shift+arrows yield to instant double; grid jump unchanged elsewhere', () => {
	assert.equal(
		resolve(key('ArrowLeft', { shiftKey: true }), { gridJump: true, libraryFocus: true }),
		null
	);
	assert.deepEqual(resolve(key('ArrowLeft')), { kind: 'grid-jump', bars: -4, repeat: false });
	assert.deepEqual(
		resolve(key('ArrowLeft', { shiftKey: true }), { gridJump: true, libraryFocus: false }),
		{
			kind: 'grid-jump',
			bars: -8,
			repeat: false
		}
	);
});

test('WASD: Cmd+D mirrors Right; Cmd+A mirrors Left only after a deck press', () => {
	assert.equal(resolve(key('d')).bars, 4);
	assert.equal(resolve(key('D', { shiftKey: true })).bars, 8);
	assert.equal(resolve(key('a')), null, 'Cmd+A stays Select all for the library');
	assert.equal(resolve(key('a'), { gridJump: true, deckFocus: true }).bars, -4);
});

test('Cmd+A over a library title keeps Select all when deckFocus is stale', () => {
	const titleTarget = { closest: (sel) => (sel.includes('title-text') ? {} : null) };
	assert.equal(
		resolve(key('a', { target: titleTarget }), { gridJump: true, deckFocus: true }),
		null
	);
});

test('text fields keep their own keys; Alt chords are left for shadow decks', () => {
	assert.equal(resolve(key('ArrowRight', { target: TEXT })), null);
	assert.equal(resolve(key('d', { target: TEXT })), null);
	assert.equal(resolve(key('ArrowRight', { altKey: true })), null);
	assert.equal(resolve(key('ArrowRight', { metaKey: false })), null, 'a bare arrow is not a jump');
});

test('Cmd+W and Cmd+S never become deck keys', () => {
	assert.equal(resolve(key('w'), { gridJump: true, deckFocus: true }), null);
	assert.equal(resolve(key('s'), { gridJump: true, deckFocus: true }), null);
});

// The handlers below are stand-ins on purpose: this test pins the routing
// contract (one press = one jump, repeats swallowed). The real wiring from a
// handler to the dispatcher is pinned in the source test that follows, and
// the dispatcher's `grid_jump` parser and engine path are covered by the
// IPC and CLI parity tests. A node test cannot load the real engine here.
test('routing: a held key fires one jump, repeats are swallowed', () => {
	const calls = [];
	let prevented = 0;
	let stopped = 0;
	const handlers = {
		toggleRecentPlay() {},
		resizeLast() {},
		exitLast() {},
		armPinPlacement() {},
		gridJump: (bars) => calls.push(bars)
	};
	const ev = (repeat) => ({
		...key('ArrowRight'),
		repeat,
		timeStamp: 0,
		preventDefault: () => prevented++,
		stopPropagation: () => stopped++
	});
	assert.equal(routing.handlePerformanceShortcutKeydown(ev(false), handlers, ON), true);
	assert.equal(routing.handlePerformanceShortcutKeydown(ev(true), handlers, ON), true);
	assert.deepEqual(calls, [4]);
	assert.equal(prevented, 2);
	assert.equal(stopped, 2, 'the chord never falls through to Select all or a bookmark');
});

test('wiring: the real hotkey handler sends grid_jump through the dispatcher', async () => {
	const { readFile } = await import('node:fs/promises');
	const source = await readFile('src/lib/rb/performance-hotkeys.ts', 'utf8');
	assert.match(source, /gridJump: \(bars\) => \{\s*void _gridJump\(bars\);/);
	assert.match(source, /runPerformanceCommandFromUi\(\{ type: 'grid_jump', deck, bars \}\)/);
});
