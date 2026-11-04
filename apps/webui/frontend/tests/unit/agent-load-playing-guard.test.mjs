// requirement: AGENT-8332, AGENT-8332b
/**
 * AGENT-8332/54 (Tue 3 Nov 2026): an agent's `load` onto a playing deck was
 * refused by the replacement guard, but the step carried only a string, and the
 * CLI's verdict line read "unconfirmed - the page accepted the order". The step
 * now names the refusal (`code: deck_playing`, the deck's track and the exact
 * override), and `replace_playing: true` ejects the deck before loading.
 *
 * [if] an agent loads onto a playing deck [then ⛔] the step is failed with
 *   code deck_playing, the playing track, and an override carrying
 *   replace_playing: true, and the deck still holds its track.
 * [if] the same load carries replace_playing: true [then ⛔] the guard no
 *   longer refuses it: the deck is ejected first and the load goes on.
 * [if] an error is not the playing guard [then ⛔] no code is attached.
 */
import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

globalThis.location ??= { search: '' };

const entry = await loadTypeScriptModule('tests/unit/fixtures/agent-load-guard-entry.ts');

function playOnDeck1() {
	const st = entry.deckStates[1];
	Object.assign(st, { stable_id: 'old-track', title: 'Old Track', artist: 'Old Artist', playing: true, audible: true, position_ms: 61234.4 });
}

afterEach(() => {
	Object.assign(entry.deckStates[1], { stable_id: null, title: null, artist: null, playing: false, audible: false, position_ms: 0 });
	delete globalThis.window;
});

test('a load onto a playing deck answers deck_playing with the track and the override', async () => {
	globalThis.window = { setTimeout: (fn, ms) => setTimeout(fn, ms) };
	const uninstall = entry.installPerformanceBrowserIpc();
	try {
		playOnDeck1();
		const result = await entry.executeAgentOrder({
			kind: 'single',
			payload: { type: 'load', deck: 1, stable_id: 'new-track' }
		});
		const [step] = result.steps;
		assert.equal(step.status, 'failed');
		assert.equal(step.code, 'deck_playing');
		assert.match(step.error, /deck 1 must be fully stopped before replacement/);
		assert.deepEqual(
			{ ...step.detail, active_state: step.detail.active_state.slice(0, 2) },
			{
				deck: 1,
				stable_id: 'old-track',
				title: 'Old Track',
				artist: 'Old Artist',
				position_ms: 61234,
				active_state: ['playing', 'audible'],
				override: { type: 'load', deck: 1, stable_id: 'new-track', replace_playing: true }
			}
		);
		assert.equal(entry.deckStates[1].stable_id, 'old-track', 'a refused load leaves the playing track on the deck');
	} finally {
		uninstall();
	}
});

test('replace_playing ejects the playing deck, so the guard no longer refuses the load', async () => {
	globalThis.window = { setTimeout: (fn, ms) => setTimeout(fn, ms) };
	const uninstall = entry.installPerformanceBrowserIpc();
	try {
		playOnDeck1();
		const result = await entry.executeAgentOrder({
			kind: 'single',
			payload: { type: 'load', deck: 1, stable_id: 'new-track', replace_playing: true }
		});
		const [step] = result.steps;
		// No audio graph or library in a unit test: the load itself fails at its
		// fetch. What matters is that it got PAST the guard after the eject.
		assert.equal(step.code, undefined, `the guard still refused: ${step.error}`);
		assert.doesNotMatch(String(step.error), /must be fully stopped/);
		assert.equal(entry.deckStates[1].playing, false, 'the eject stopped the deck');
		assert.notEqual(entry.deckStates[1].stable_id, 'old-track', 'the eject took the old track off');
	} finally {
		uninstall();
	}
});

test('only the playing guard is reported as deck_playing, and only load carries an override', () => {
	const state = { decks: { 1: { stable_id: 'a', title: 'A', artist: null, position_ms: 10, playing: true, audible: true } } };
	assert.equal(entry.deckPlayingRefusal(new Error('load: network'), { type: 'load', deck: 1, stable_id: 'b' }, state), null);
	const pick = entry.deckPlayingRefusal(new entry.DeckReplacementBlockedError(1, ['playing']), { type: 'cmdk_pick', index: 0 }, state);
	assert.equal(pick.code, 'deck_playing');
	assert.equal(pick.detail.override, null);
	assert.deepEqual(entry.playingDeckReplaced({ type: 'load', deck: 1, stable_id: 'b', replace_playing: true }, state), {
		deck: 1, stable_id: 'a', title: 'A', artist: null, position_ms: 10
	});
	assert.equal(entry.playingDeckReplaced({ type: 'load', deck: 1, stable_id: 'b' }, state), null);
});
