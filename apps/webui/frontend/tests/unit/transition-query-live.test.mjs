/**
 * TRANS-01: queryPerformanceState().transition matches readTransition() on
 * the live mixer + deck stores. No stubbed transitioning.
 *
 * [if] decks 1 and 2 are both active and the default mixer is at xfader 0.5
 *   with faders at 1 [then] query().transition.state === 'transitioning'
 * [if] the xfader then parks at 0 and only bus A remains heard [then] state
 *   is not transitioning on that snapshot
 */
import assert from 'node:assert/strict';
import { afterEach, before, test } from 'node:test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

let graph;

before(async () => {
	graph = await loadTypeScriptModule('tests/unit/fixtures/transition-query-entry.ts');
});

function restore() {
	graph.mixerState.crossfader = 0.5;
	for (const id of [1, 2, 3, 4]) {
		graph.deckStates[id].stable_id = null;
		graph.deckStates[id].playing = false;
		graph.deckStates[id].audible = false;
		graph.mixerState.channels[id].fader = 1;
	}
}

afterEach(restore);

function armDualDeckBlend() {
	for (const id of [1, 2]) {
		graph.deckStates[id].stable_id = `track-${id}`;
		graph.deckStates[id].playing = true;
		graph.deckStates[id].audible = true;
	}
}

test('queryPerformanceState publishes transition from readTransition', () => {
	const source = readFileSync(
		fileURLToPath(new URL('../../src/lib/rb/performance-ipc.svelte.ts', import.meta.url)),
		'utf8'
	);
	assert.match(source, /transition:\s*readTransition\(\)/);
});

test('default stores query as idle', () => {
	assert.equal(graph.queryPerformanceState().transition.state, 'idle');
	assert.deepEqual(graph.queryPerformanceState().transition, graph.readTransition());
});

test('live dual-deck blend through query() is transitioning', () => {
	armDualDeckBlend();
	const queried = graph.queryPerformanceState().transition;
	const read = graph.readTransition();
	assert.equal(queried.state, 'transitioning');
	assert.deepEqual(queried, read);
	assert.notEqual(queried.incoming_deck, null);
	assert.notEqual(queried.outgoing_deck, null);
	assert.notEqual(queried.incoming_deck, queried.outgoing_deck);
});

test('parking the live xfader at 0 clears transitioning on the next query', () => {
	armDualDeckBlend();
	assert.equal(graph.queryPerformanceState().transition.state, 'transitioning');
	graph.mixerState.crossfader = 0;
	assert.notEqual(graph.queryPerformanceState().transition.state, 'transitioning');
});

test('structuredClone of transition succeeds and does not alias the next read', () => {
	armDualDeckBlend();
	const queried = graph.queryPerformanceState().transition;
	assert.equal(queried.state, 'transitioning');
	const cloned = structuredClone(queried);
	assert.deepEqual(cloned, queried);
	cloned.state = 'idle';
	cloned.incoming_deck = null;
	assert.equal(graph.readTransition().state, 'transitioning');
	assert.equal(graph.queryPerformanceState().transition.state, 'transitioning');
});
