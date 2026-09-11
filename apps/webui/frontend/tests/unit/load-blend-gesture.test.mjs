/**
 * Load-to-CH blend pointer session (issue #286). Coordinates only, no DOM.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let createLoadBlendSession;

before(async () => {
	({ createLoadBlendSession } = await loadTypeScriptModule('src/lib/rb/load-blend-gesture.ts'));
});

test('down + up with distance 0 is a click, never morph', () => {
	const session = createLoadBlendSession({
		incomingDeck: 2,
		stableId: 'track-b',
		masterDeck: 1
	});
	session.pointerDown(10, 10);
	assert.equal(session.phase, 'down');
	const move = session.pointerMove(10, 10);
	assert.equal(move.kind, 'hold');
	assert.notEqual(session.phase, 'morph');
	const up = session.pointerUp();
	assert.equal(up.kind, 'click');
	assert.equal(session.phase, 'done');
});

test('down + move 9 px enters morph', () => {
	const session = createLoadBlendSession({
		incomingDeck: 2,
		stableId: 'track-b',
		masterDeck: 1
	});
	session.pointerDown(0, 0);
	const move = session.pointerMove(9, 0);
	assert.equal(move.kind, 'morph');
	assert.equal(move.entered, true);
	assert.equal(session.phase, 'morph');
});

test('masterDeck null or equal to incoming refuses and stays non-morph', () => {
	const none = createLoadBlendSession({
		incomingDeck: 2,
		stableId: 'track-b',
		masterDeck: null
	});
	none.pointerDown(0, 0);
	const refusedNone = none.pointerMove(9, 0);
	assert.equal(refusedNone.kind, 'refuse');
	assert.notEqual(none.phase, 'morph');
	assert.equal(none.pointerUp().kind, 'click');

	const same = createLoadBlendSession({
		incomingDeck: 1,
		stableId: 'track-b',
		masterDeck: 1
	});
	same.pointerDown(0, 0);
	const refusedSame = same.pointerMove(0, -9);
	assert.equal(refusedSame.kind, 'refuse');
	assert.match(refusedSame.reason, /master/i);
	assert.notEqual(same.phase, 'morph');
});

test('abort from morph is aborted', () => {
	const session = createLoadBlendSession({
		incomingDeck: 3,
		stableId: 'track-c',
		masterDeck: 1
	});
	session.pointerDown(0, 0);
	session.pointerMove(0, -9);
	assert.equal(session.phase, 'morph');
	const aborted = session.abort();
	assert.equal(aborted.kind, 'restore');
	assert.equal(session.phase, 'aborted');
});

test('after pointerUp in morph, further moves are ignored (done)', () => {
	const session = createLoadBlendSession({
		incomingDeck: 3,
		stableId: 'track-c',
		masterDeck: 1
	});
	session.pointerDown(0, 0);
	session.pointerMove(12, -20);
	assert.equal(session.phase, 'morph');
	assert.equal(session.pointerUp().kind, 'morph');
	assert.equal(session.phase, 'done');
	const later = session.pointerMove(80, -80);
	assert.equal(later.kind, 'ignore');
	assert.equal(session.phase, 'done');
});
