// requirement: KEYB-14023
/**
 * KEYB-13755 (K3 / #9395): the one live keyboard selector session (selector-session.ts).
 *
 * Regression lines:
 * - if the first key travels instead of revealing the start node then broken
 * - if an unknown start id does not fall back to the first root then broken
 * - if an empty registry does not fail loud then broken
 * - if a registry rebuild drops a selection whose node survived then broken
 * - if clear on an empty session reports a clear or hides the ring then broken
 * - if a removed selected node stays selected, or the ring stays on it, then broken
 * - if a layout change that removes every node leaves the ring drawn then broken
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const sessions = await loadTypeScriptModule('src/lib/keyboard/selector-session.ts');

const node = (id, parentId, kind, x, y) => ({
	id,
	parentId,
	kind,
	rect: { x, y, width: 100, height: 100 }
});

const ROW = [
	node('row', null, 'section', 0, 0),
	node('left', 'row', 'leaf', 0, 0),
	node('mid', 'row', 'leaf', 120, 0),
	node('right', 'row', 'leaf', 240, 0)
];

/** A session over a fixture node list, recording every ring show and hide. */
function sessionOver(nodes = ROW) {
	const state = { version: 1, nodes, rings: [] };
	const session = sessions.createSelectorSession({
		snapshot: () => state.nodes,
		version: () => state.version,
		showRing: (target) => state.rings.push(target.id),
		hideRing: () => state.rings.push(null)
	});
	return { state, session };
}

// [if] the first key arrives on a cleared session [then] it only reveals the start node, the next key moves, [else stop].
test('the first selector key reveals the start node; the second moves', () => {
	const { state, session } = sessionOver();
	assert.equal(session.run('selector.move.right', { timeStamp: 0, startId: 'left' }), 'left');
	assert.equal(session.run('selector.move.right', { timeStamp: 1, startId: null }), 'mid');
	assert.deepEqual(state.rings, ['left', 'mid']);
	assert.equal(session.selectionWithin('row'), true);
	assert.equal(session.selectionWithin('right'), false);
});

// [if] the start id is not a registered node [then] the session starts on the first root, [else stop].
test('an unknown start id falls back to the first root', () => {
	const { session } = sessionOver();
	assert.equal(session.run('selector.drill-in', { timeStamp: 0, startId: 'gone' }), 'row');
});

// [if] a selector key arrives with no registered nodes [then] the session throws, [else stop].
test('a selector key with no registered nodes fails loud', () => {
	const { session } = sessionOver([]);
	assert.throws(
		() => session.run('selector.move.up', { timeStamp: 0, startId: null }),
		/no selector nodes are registered/
	);
});

// [if] the registry version moves and the selected node survives [then] the rebuilt tree keeps it, [else stop].
test('a registry rebuild keeps a selection whose node survived', () => {
	const { state, session } = sessionOver();
	session.run('selector.move.right', { timeStamp: 0, startId: 'mid' });
	state.nodes = ROW.filter((n) => n.id !== 'left');
	state.version += 1;
	assert.equal(session.run('selector.move.left', { timeStamp: 1, startId: null }), 'mid');
	assert.equal(session.run('selector.move.right', { timeStamp: 2, startId: null }), 'right');
});

// [if] clear runs on an empty then a live session [then] only the live one reports true and hides the ring, [else stop].
test('clear reports false when empty and hides the ring once when live', () => {
	const { state, session } = sessionOver();
	assert.equal(session.clear(), false);
	assert.deepEqual(state.rings, []);
	session.run('selector.move.up', { timeStamp: 0, startId: 'left' });
	assert.equal(session.clear(), true);
	assert.equal(session.selected(), null);
	assert.deepEqual(state.rings, ['left', null]);
});

const DEEP = [
	node('decks', null, 'section', 0, 0),
	node('deck.3', 'decks', 'group', 0, 0),
	node('deck.3.transport', 'deck.3', 'leaf', 0, 0),
	node('deck.1', 'decks', 'group', 120, 0)
];

// [if] a layout change removes the selected leaf and its parent [then] reconcile selects the nearest surviving ancestor, [else stop].
test('a removed selection moves to its nearest surviving ancestor and the ring follows', () => {
	const { state, session } = sessionOver(DEEP);
	session.run('selector.drill-in', { timeStamp: 0, startId: 'deck.3.transport' });
	assert.equal(session.selectedLeaf(), 'deck.3.transport');
	state.nodes = DEEP.filter((n) => !n.id.startsWith('deck.3'));
	state.version += 1;
	assert.equal(session.reconcile(), 'decks');
	assert.deepEqual(state.rings, ['deck.3.transport', 'decks']);
	assert.equal(session.selectedLeaf(), null, 'the removed leaf is no command target');
	assert.equal(session.selectionWithin('deck.3'), false);
	assert.equal(session.reconcile(), 'decks', 'an unchanged registry does not republish');
	assert.deepEqual(state.rings, ['deck.3.transport', 'decks']);
});

// [if] the selected node is removed and a read comes before any reconcile [then] the read already reports the ancestor, [else stop].
test('a read after removal reports the surviving ancestor, not the removed id', () => {
	const { state, session } = sessionOver(DEEP);
	session.run('selector.move.up', { timeStamp: 0, startId: 'deck.3' });
	state.nodes = DEEP.filter((n) => n.id !== 'deck.3' && n.id !== 'deck.3.transport');
	state.version += 1;
	assert.equal(session.selected(), 'decks');
	assert.equal(session.run('selector.drill-in', { timeStamp: 1, startId: null }), 'deck.1');
});

// [if] a layout change removes every node [then] reconcile clears the session and hides the ring, [else stop].
test('a layout change that removes every node clears the session', () => {
	const { state, session } = sessionOver(DEEP);
	session.run('selector.move.up', { timeStamp: 0, startId: 'deck.1' });
	state.nodes = [];
	state.version += 1;
	assert.equal(session.reconcile(), null);
	assert.equal(session.selected(), null);
	assert.deepEqual(state.rings, ['deck.1', null]);
});
