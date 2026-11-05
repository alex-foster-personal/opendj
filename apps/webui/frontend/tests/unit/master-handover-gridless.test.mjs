/**
 * PLAY-28: a master handover to a GRIDLESS deck mixes without Beat Sync, it is
 * never refused.
 *
 * Wed 7 Oct 2026 16:11:29Z, silver, packaged build 2411a8f23, Beat Sync Max on:
 * "auto-play: deck 1 master handover refused: Beat Sync: deck 1 requires a
 * valid real PQTZ beat grid: RangeError: beat grid must contain at least 2
 * beats, got 0". The incoming track had no grid; setDeckMaster tried to lock
 * the outgoing synced deck to it, the plain grid error reverted the master,
 * and AutoPlay never queued another track.
 *
 * [if] the new master has 0 beats, 1 beat or no analysis and a synced deck
 *   follows [then] the handover mixes without Beat Sync [⛔️ if it still syncs].
 * [if] the new master has a trusted grid [then] it syncs as before
 *   [⛔️ if a valid grid is ignored].
 * [if] nobody follows [then] there is nothing to announce [⛔️ if a lone deck nags].
 * [if] setDeckMaster or the automatic re-join reaches _synchronizeFollowers
 *   [then] it asked handoverMixesWithoutBeatSync first [⛔️ if the refusal path is back].
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { engineBlockAfter, readFrontendSource } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const REAL_PQTZ_BEATS = [
	{ n: 1, bpm: 127, t: 0.135 },
	{ n: 2, bpm: 127, t: 0.608 },
	{ n: 3, bpm: 127, t: 1.08 },
	{ n: 4, bpm: 127, t: 1.553 }
];
const withGrid = (beats) => ({ beatgrid: { source: 'rekordbox', beats, status: 'ok' } });

let grid;
before(async () => {
	grid = await loadTypeScriptModule('src/lib/player/grid-features.ts');
});

test('[PLAY-28] a gridless new master with a follower mixes without Beat Sync', () => {
	for (const [label, anlz] of [
		['0 beats (the silver 16:11Z incoming track)', withGrid([])],
		['1 beat', withGrid(REAL_PQTZ_BEATS.slice(0, 1))],
		['no analysis', null]
	]) {
		assert.equal(
			grid.handoverMixesWithoutBeatSync({ anlz }, 1),
			true,
			`${label}: if the handover still tries to sync then "master handover refused" comes back`
		);
	}
});

test('[PLAY-28] control: a gridded new master still syncs, and a lone deck needs no notice', () => {
	assert.equal(
		grid.handoverMixesWithoutBeatSync({ anlz: withGrid(REAL_PQTZ_BEATS) }, 1),
		false,
		'a valid grid must never be ignored'
	);
	assert.equal(
		grid.handoverMixesWithoutBeatSync({ anlz: withGrid([]) }, 0),
		false,
		'no followers, nothing to mix without'
	);
});

test('[PLAY-28] the notice is honest and names the deck', () => {
	assert.equal(grid.gridlessMasterNotice(1), 'Deck 1 has no beat grid: mixed without Beat Sync');
	assert.equal(grid.GRIDLESS_MASTER_PERF_KIND, 'beat-sync-gridless-master');
});

test('[PLAY-28] SHAPE GUARD: setDeckMaster and the automatic re-join ask before syncing to the new master', () => {
	const setMaster = engineBlockAfter(
		'	async setDeckMaster(deck: DeckId, options?: { lock?: boolean }): Promise<void> {'
	);
	const ask = setMaster.indexOf('handoverMixesWithoutBeatSync(st, followers.length)');
	const sync = setMaster.indexOf('await _synchronizeFollowers(deck, followers');
	assert.ok(ask >= 0, 'if setDeckMaster stops asking then a gridless handover is refused again');
	assert.ok(sync > ask, 'the gridless check must come BEFORE the sync it short-circuits');
	assert.match(
		setMaster,
		/_mixWithoutBeatSync\(deck, followers, pushToast, recordPerfEvent, _clearSyncError\)/
	);
	const audible = setMaster.slice(setMaster.indexOf('masterSwitchFollowers'));
	const stop = audible.indexOf('!st.playing && !_rt[deck].desiredActive');
	const bump = audible.indexOf('_bumpReanchorOperation(deck)');
	const assign = audible.indexOf("_assignMaster(deck, 'manual')");
	assert.ok(
		stop >= 0,
		'if the audible branch lost the stopping-deck return then a MASTER click during pause-tail locks the stopping deck'
	);
	assert.ok(bump > stop, 'stopping-deck return must come BEFORE reanchor');
	assert.ok(assign > stop, 'stopping-deck return must come BEFORE assign');

	const engine = readFrontendSource('src/lib/rb/audio-engine.svelte.ts');
	assert.match(
		engine,
		/if \(handoverMixesWithoutBeatSync\(deckStates\[next\], followers\.length\)\) return _mixWithoutBeatSync\(next, followers, pushToast, recordPerfEvent, _clearSyncError\);\s*\/\/ PLAY-28\s*await _synchronizeFollowers\(next, followers/,
		'the automatic re-join after an election must take the same path'
	);
	const gridless = readFrontendSource('src/lib/player/grid-features.ts');
	const helperAt = gridless.indexOf('export function mixWithoutBeatSync(');
	assert.ok(
		helperAt >= 0,
		'if the gridless mix notice left grid-features then the handover is silent again'
	);
	const helper = gridless.slice(helperAt, gridless.indexOf('\n}', helperAt));
	assert.match(helper, /pushToast\(notice, 'warn'\)/, 'the fallback must be visible');
	assert.match(
		helper,
		/recordPerfEvent\(GRIDLESS_MASTER_PERF_KIND/,
		'the fallback must be in telemetry'
	);
});
