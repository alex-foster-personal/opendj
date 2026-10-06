/**
 * PLAY-25: a gridless MASTER never stops a follower from starting.
 *
 * Tue 6 Oct 2026, silver preview 17:35:26Z, Beat Sync Max on: AutoPlay loaded
 * 0a5d7e2250 on deck 1, then the handoff failed with "Beat Sync: deck 2
 * requires a valid real PQTZ beat grid: RangeError: beat grid must contain at
 * least 2 beats, got 0". Deck 2 was the playing master with no grid; `play` on
 * deck 1 tried to join its phase, threw, the follower never started, and the
 * room went silent when deck 2 ended.
 *
 * [if] the master's grid is missing or degenerate [then] the follower may not
 *   phase-lock to it and plays unsynced [⛔️ if play still joins its phase].
 * [if] both decks carry a trusted grid [then] the follower still syncs
 *   [⛔️ if Beat Sync stopped working for normal tracks].
 * [if] play joins a master's phase anywhere [then] it first asks canPhaseLockTo
 *   about THAT master [⛔️ if any join branch still reads only the follower].
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { engineBlockAfter } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const REAL_PQTZ_BEATS = [
	{ n: 1, bpm: 127, t: 0.135 },
	{ n: 2, bpm: 127, t: 0.608 },
	{ n: 3, bpm: 127, t: 1.08 },
	{ n: 4, bpm: 127, t: 1.553 }
];
const withGrid = (beats) => ({ beatgrid: { source: 'rekordbox', beats, status: 'ok' } });
const deck = (anlz, beat_sync_enabled = true) => ({ deck_id: 1, stable_id: 'sid', beat_sync_enabled, anlz });

let grid;
before(async () => {
	grid = await loadTypeScriptModule('src/lib/player/grid-features.ts');
});

test('[PLAY-25] a master with 0 beats, 1 beat or no analysis cannot be phase-locked to', () => {
	const follower = deck(withGrid(REAL_PQTZ_BEATS));
	for (const [label, master] of [
		['0 beats (the silver 17:35Z master)', deck(withGrid([]))],
		['1 beat', deck(withGrid(REAL_PQTZ_BEATS.slice(0, 1)))],
		['no analysis', deck(null)]
	]) {
		assert.equal(grid.canPhaseLockTo(follower, master), false, `${label}: if play joins its phase then the handoff throws`);
	}
});

test('[PLAY-25] control: two gridded decks still phase-lock, and a gridless or unsynced follower does not', () => {
	const master = deck(withGrid(REAL_PQTZ_BEATS));
	assert.equal(grid.canPhaseLockTo(deck(withGrid(REAL_PQTZ_BEATS)), master), true);
	assert.equal(grid.canPhaseLockTo(deck(withGrid([])), master), false);
	assert.equal(grid.canPhaseLockTo(deck(withGrid(REAL_PQTZ_BEATS), false), master), false);
});

test('[PLAY-25] SHAPE GUARD: every phase-join branch in play asks canPhaseLockTo about its master', () => {
	const body = engineBlockAfter('	async play(deck: DeckId, pressT0Ms?: number, startAtContextSec?: number): Promise<void> {');
	const joins = body.split('await _synchronizeFollowers(').length - 1;
	assert.ok(joins >= 3, `expected the three phase-join branches in play, found ${joins}`);
	assert.match(body, /syncClock !== null && canPhaseLockTo\(st, deckStates\[syncClock\]\)/);
	assert.match(body, /elected !== deck && canPhaseLockTo\(st, deckStates\[elected\]\)/);
	assert.match(
		body,
		/syncClock === deck \|\| !syncActive \|\| \(syncClock !== null && !canPhaseLockTo\(st, deckStates\[syncClock\]\)\)/,
		'if the last branch can still join a gridless master then the 17:35Z dead air comes back'
	);
	assert.doesNotMatch(body, /&& syncActive\) \{\s*await _synchronizeFollowers/, 'no join may be gated on the follower alone');
	assert.match(body, /\[beat-sync\] deck \$\{deck\} starts unsynced: master deck \$\{syncClock\} has no usable beat grid/);
});
