/**
 * PLAY-28 follow-through: after a gridless master handover, a later tempo or
 * seek must not enter the beat-grid sync path.
 *
 * Wed 7 Oct 2026, Codex P1 on PR 5883: with Beat Sync Max on, the handover
 * skipped only that one synchronization. `_masterDeck` still pointed at the
 * gridless deck, so `setTempoRatio` rejected the follower's pitch and a
 * waveform or beat seek on either deck called `_synchronizeFollowers`, which
 * threw the same `requireBeatGrid` error the fallback exists to avoid.
 *
 * [if] the master has no trusted grid [then] the sync clock is null, a later
 *   seek on either deck plans free transport, and sync does not own the
 *   follower tempo [⛔️ if the pitch fader is rejected or the seek calls
 *   the beat-grid path].
 * [if] the master has a trusted grid [then] the clock stays that deck and
 *   seek planning still syncs [⛔️ if a normal sync is dropped].
 * [if] quantizedSeek or setTempoRatio plans a sync [then] it asks
 *   `_beatSyncMaster`, which asks `beatSyncClockMaster`, and the playing
 *   master clock itself is unchanged [⛔️ if play re-elects a gridless master
 *   or a later control still reads the raw master].
 * [if] the handover mixes without Beat Sync [then] each follower's sync_error
 *   is cleared [⛔️ if a stale sync error stays on screen].
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
const playing = [
	{ id: 1, playing: true, beatSyncEnabled: false },
	{ id: 2, playing: true, beatSyncEnabled: true }
];

let grid;
let decisions;
before(async () => {
	grid = await loadTypeScriptModule('src/lib/player/grid-features.ts');
	decisions = await loadTypeScriptModule('src/lib/rb/beat-sync-decisions.ts');
});

test('[PLAY-28] a gridless master is not a Beat Sync clock for a later tempo or seek', () => {
	for (const [label, anlz] of [
		['0 beats', withGrid([])],
		['1 beat', withGrid(REAL_PQTZ_BEATS.slice(0, 1))],
		['no analysis', null]
	]) {
		const clock = grid.beatSyncClockMaster(1, { anlz });
		assert.equal(clock, null, `${label}: if the clock stays the gridless deck then the next seek calls requireBeatGrid`);
		assert.equal(
			decisions.planSeekSync({
				deck: 2,
				transportActive: true,
				beatSyncEnabled: true,
				activeMaster: clock,
				beatSyncMax: true,
				candidates: playing
			}).kind,
			'free',
			`${label}: a follower seek must not re-enter the beat-grid sync path`
		);
		assert.equal(
			decisions.planSeekSync({
				deck: 1,
				transportActive: true,
				beatSyncEnabled: false,
				activeMaster: clock,
				beatSyncMax: true,
				candidates: playing
			}).kind,
			'free',
			`${label}: a master seek under Beat Sync Max must not re-anchor followers through the grid`
		);
		assert.equal(decisions.syncMayWriteTempo(2, true, clock), false, `${label}: follower pitch must not be rejected as synced`);
	}
});

test('[PLAY-28] control: the unfiltered clock still syncs, and a trusted grid keeps the clock', () => {
	const raw = 1;
	assert.equal(
		decisions.planSeekSync({
			deck: 2,
			transportActive: true,
			beatSyncEnabled: true,
			activeMaster: raw,
			beatSyncMax: true,
			candidates: playing
		}).kind,
		'follower',
		'the regression is the filter: passing the raw master id still enters beat-grid sync'
	);
	assert.equal(
		decisions.planSeekSync({
			deck: 1,
			transportActive: true,
			beatSyncEnabled: false,
			activeMaster: raw,
			beatSyncMax: true,
			candidates: playing
		}).kind,
		'master-max',
		'Beat Sync Max on the raw clock still re-anchors followers'
	);
	const clock = grid.beatSyncClockMaster(1, { anlz: withGrid(REAL_PQTZ_BEATS) });
	assert.equal(clock, 1, 'a trusted grid must stay the sync clock');
	const synced = [
		{ id: 1, playing: true, beatSyncEnabled: true },
		{ id: 2, playing: true, beatSyncEnabled: true }
	];
	assert.equal(
		decisions.planSeekSync({
			deck: 2,
			transportActive: true,
			beatSyncEnabled: true,
			activeMaster: clock,
			beatSyncMax: true,
			candidates: synced
		}).kind,
		'follower'
	);
	assert.equal(
		decisions.planSeekSync({
			deck: 1,
			transportActive: true,
			beatSyncEnabled: true,
			activeMaster: clock,
			beatSyncMax: true,
			candidates: synced
		}).kind,
		'master-max'
	);
	assert.equal(decisions.syncMayWriteTempo(2, true, clock), true);
	assert.equal(decisions.syncMayWriteTempo(1, true, clock), false, 'the master deck is never a follower');
	assert.equal(grid.beatSyncClockMaster(null, { anlz: withGrid(REAL_PQTZ_BEATS) }), null);
});

test('[PLAY-28] SHAPE GUARD: later tempo and seek ask the gridless-aware clock, and play still sees a playing master', () => {
	const beatSyncMaster = engineBlockAfter('function _beatSyncMaster(): DeckId | null {');
	assert.match(beatSyncMaster, /const master = _syncClockMaster\(\)/);
	assert.match(beatSyncMaster, /return master === null \? null : beatSyncClockMaster\(master, deckStates\[master\]\)/);

	const playingClock = engineBlockAfter('function _syncClockMaster(): DeckId | null {');
	assert.match(
		playingClock,
		/return _masterDeck !== null && deckStates\[_masterDeck\]\.playing \? _masterDeck : null/
	);
	assert.doesNotMatch(
		playingClock,
		/beatSyncClockMaster/,
		'nulling the playing-master clock makes play() re-elect and can take master off the gridless deck'
	);

	const seek = engineBlockAfter(
		'	async quantizedSeek(deck: DeckId, ms: number, skipGridQuantize = false, pressT0Ms?: number, jumpBeats?: number | null): Promise<void> {'
	);
	assert.match(seek, /const activeMaster = _beatSyncMaster\(\)/);
	assert.doesNotMatch(seek, /const activeMaster = _syncMaster\(\)/);

	const tempo = engineBlockAfter('	async setTempoRatio(deck: DeckId, ratio: number): Promise<void> {');
	assert.match(tempo, /const activeMaster = _beatSyncMaster\(\)/);
	assert.match(tempo, /activeMaster === deck/);
	assert.doesNotMatch(
		tempo,
		/_masterDeck === deck/,
		'a gridless master pitch change must not gather followers for _synchronizeFollowers'
	);

	const owns = engineBlockAfter('function _syncOwnsFollowerTempo(deck: DeckId): boolean {');
	assert.match(owns, /_masterDeck/);
	assert.match(owns, /effectiveBeatSync\(deckStates\[deck\]\)/);
	assert.match(owns, /beatSyncClockMaster\(master, deckStates\[master\]\)/);

	const mode = engineBlockAfter('	setSyncMode(deck: DeckId, mode: SyncMode): Promise<void> {');
	assert.match(mode, /const master = _beatSyncMaster\(\)/);

	const beatSync = engineBlockAfter('	setBeatSync(deck: DeckId, enabled: boolean): Promise<void> {');
	assert.match(
		beatSync,
		/if \(master !== null && beatSyncClockMaster\(master, deckStates\[master\]\) === null\) return Promise\.resolve\(\)/
	);
});

test('[PLAY-28] the unsynced mix clears a stale follower sync_error', () => {
	const cleared = [];
	grid.mixWithoutBeatSync(
		1,
		[2, 3],
		() => {},
		() => {},
		(deck) => {
			cleared.push(deck);
		}
	);
	assert.deepEqual(cleared, [2, 3], 'if a previous sync_error stays, the UI still shows a failure for an intentional unsynced mix');

	const setMaster = engineBlockAfter('	async setDeckMaster(deck: DeckId, options?: { lock?: boolean }): Promise<void> {');
	assert.match(setMaster, /_mixWithoutBeatSync\(deck, followers, pushToast, recordPerfEvent, _clearSyncError\)/);
	const engine = engineBlockAfter('function _electPlayingMaster(options?: { force?: boolean; reason?: MasterReason; to?: DeckId; levels?: { masterDb: number; toDb: number | null } }): DeckId | null {');
	assert.match(
		engine,
		/_mixWithoutBeatSync\(next, followers, pushToast, recordPerfEvent, _clearSyncError\)/
	);
});
