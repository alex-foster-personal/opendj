// requirement: KEYB-14059
/**
 * #12253: the selector's one polite announcement (selector-announce.ts), alone and driven by
 * the live selector session.
 *
 * Regression lines:
 * - if a move that changes the selected node does not announce its name then broken
 * - if a move with no eligible neighbour announces anything then broken
 * - if clearing the selector does not say "Selection cleared" exactly once then broken
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const announce = await loadTypeScriptModule('src/lib/keyboard/selector-announce.ts');
const sessions = await loadTypeScriptModule('src/lib/keyboard/selector-session.ts');

const node = (id, parentId, kind, x) => ({
	id,
	parentId,
	kind,
	rect: { x, y: 0, width: 100, height: 100 }
});

const ROW = [
	node('row', null, 'section', 0),
	node('left', 'row', 'leaf', 0),
	node('right', 'row', 'leaf', 120)
];

/** A selector session whose ring writes go through one announcer, recording every message. */
function announcedSession() {
	const messages = [];
	const announcer = announce.createSelectorAnnouncer((message) => messages.push(message));
	const session = sessions.createSelectorSession({
		snapshot: () => ROW,
		version: () => 1,
		showRing: (target) => announcer.track(target.id, null),
		hideRing: () => announcer.track(null, null)
	});
	return { messages, session };
}

// [if] selector movement changes the selected node [then] its label is announced, its spoken id when unlabelled, [else stop].
test('a move to a new node announces its label, or its spoken id when it has none', () => {
	assert.equal(announce.selectorAnnouncement('deck.1', 'deck.2', 'Second deck'), 'Second deck');
	assert.equal(announce.selectorAnnouncement('deck.1', 'deck.2', null), 'Deck 2');
	assert.equal(announce.selectorAnnouncement('deck.1', 'deck.2', '  '), 'Deck 2');
	assert.equal(announce.selectorAnnouncement(null, 'deck.1', null), 'Deck 1');
});

// [if] selector movement has no eligible neighbour (same node) [then] there is no announcement, [else stop].
test('a move that keeps the same node announces nothing', () => {
	assert.equal(announce.selectorAnnouncement('deck.1', 'deck.1', 'Deck 1'), null);
	assert.equal(announce.selectorAnnouncement(null, null, null), null);
});

// [if] selector state is cleared [then] the message is exactly "Selection cleared", [else stop].
test('clearing the selection announces Selection cleared', () => {
	assert.equal(announce.SELECTOR_CLEARED_ANNOUNCEMENT, 'Selection cleared');
	assert.equal(announce.selectorAnnouncement('deck.1', null, null), 'Selection cleared');
});

// [if] a session key hits the row edge, or Esc repeats [then] nothing new is announced, [else stop].
test('session: a no-op move and a repeated clear emit nothing', () => {
	const { messages, session } = announcedSession();
	session.run('selector.move.right', { timeStamp: 0, startId: 'left' });
	session.run('selector.move.left', { timeStamp: 1, startId: null });
	assert.deepEqual(messages, ['Left']);
	session.run('selector.move.right', { timeStamp: 2, startId: null });
	session.run('selector.move.right', { timeStamp: 3, startId: null });
	assert.deepEqual(messages, ['Left', 'Right']);
	assert.equal(session.clear(), true);
	assert.equal(session.clear(), false);
	assert.deepEqual(messages, ['Left', 'Right', 'Selection cleared']);
});
