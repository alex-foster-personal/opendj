// Issue #1134: the MASTER deck's BPM was being moved by beat sync, and kept
// moving after BOTH decks' BEAT SYNC toggles were switched off, so nothing
// could lock to it.
//
// Root cause is ownership, not arithmetic. A re-anchor is not one tempo write,
// it is a RAMP of ~10 scheduled writes spread over REANCHOR_RAMP_DURATION_SEC,
// and `_continueTempoRamp` ran that sequence to completion against the roles
// captured when the ramp was planned. Nothing re-asked, per write, whether the
// deck was still a follower. So:
//
//   - dimming BEAT SYNC mid-ramp did not stop the remaining writes, and a
//     dragged pitch fader queues a fresh 10-step ramp per pointer sample, so
//     the tail kept moving a deck's BPM long after both toggles were off;
//   - promoting a ramping follower to MASTER (setDeckMaster assigned the role
//     only AFTER the sync command) left that ramp writing the tempo of the
//     deck every other deck was now supposed to be following.
//
// The fix states the rule once, as `syncMayWriteTempo`, and re-reads it at
// every write rather than once per command.
//
// Regression lines (one per assertion group below):
// - if syncMayWriteTempo returns true for the master then sync writes the
//   reference tempo and no deck can lock
// - if it returns true with BEAT SYNC off then a disabled deck's tempo is
//   still sync's to move
// - if _continueTempoRamp stops consulting live ownership per step then a
//   switched-off deck keeps being written for the rest of the ramp
// - if _synchronizeFollowers stops refusing a followers list containing the
//   master then the master can be phase-locked to itself
// - if _synchronizeFollowers stops re-reading ownership after its awaits then
//   a toggle flipped inside that window is ignored
// - if setDeckMaster assigns the master after synchronizing then the outgoing
//   master looks like the master for the whole re-anchor
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { engineBlockAfter } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://beat-sync-ownership.example.test';

let audio;

before(async () => {
	audio = await loadTypeScriptModule('src/lib/rb/audio-engine.svelte.ts', {
		viteApiBase: API_BASE
	});
});

//-----------------------------------------------------------------------------
// the rule itself
//-----------------------------------------------------------------------------

test('sync may write a follower tempo and never the master it is following', () => {
	assert.equal(typeof audio.syncMayWriteTempo, 'function');
	// Deck 1 is master, deck 2 is a lit follower: only deck 2 is sync's to move.
	assert.equal(audio.syncMayWriteTempo(2, true, 1), true);
	assert.equal(
		audio.syncMayWriteTempo(1, true, 1),
		false,
		'the master is never a follower - its tempo is its own pitch fader'
	);
	// A master whose own BEAT SYNC happens to be lit is still not a follower.
	assert.equal(audio.syncMayWriteTempo(3, true, 3), false);
});

test('sync may not write the tempo of a deck whose BEAT SYNC is off', () => {
	assert.equal(audio.syncMayWriteTempo(2, false, 1), false);
	assert.equal(audio.syncMayWriteTempo(4, false, 1), false);
});

test('with no master selected sync owns nobody', () => {
	assert.equal(audio.syncMayWriteTempo(2, true, null), false);
});

test('syncMayWriteTempo refuses inputs it cannot decide from', () => {
	assert.throws(() => audio.syncMayWriteTempo(2, 'yes', 1), TypeError);
	assert.throws(() => audio.syncMayWriteTempo(0, true, 1), RangeError);
	assert.throws(() => audio.syncMayWriteTempo(2, true, 9), RangeError);
});

//-----------------------------------------------------------------------------
// the maintainer's repro, read across the whole exported decision surface
//-----------------------------------------------------------------------------

test('two decks, deck 1 master: enabling sync on deck 2 moves deck 2 and only deck 2', () => {
	const master = 1;
	const candidates = [
		{ id: 1, playing: true, beatSyncEnabled: true },
		{ id: 2, playing: true, beatSyncEnabled: true }
	];
	assert.equal(audio.syncChangeRequiresReschedule(2, true, true, master), true);
	assert.equal(audio.syncMayWriteTempo(2, true, master), true);
	assert.equal(audio.syncMayWriteTempo(1, true, master), false);
	// A master relocate pushes its followers; it never pulls its own tempo.
	assert.deepEqual(audio.beatSyncMaxFollowers(master, true, master, candidates), [2]);
	assert.ok(!audio.beatSyncMaxFollowers(master, true, master, candidates).includes(master));
});

test('with BEAT SYNC off on both decks, nudging deck 2 writes deck 2 alone and the master is untouched', () => {
	const master = 1;
	// Both toggles off - this is the exact state in issue #1134.
	const activity = {
		1: { playing: true, beat_sync_enabled: false },
		2: { playing: true, beat_sync_enabled: false },
		3: { playing: false, beat_sync_enabled: false },
		4: { playing: false, beat_sync_enabled: false }
	};
	const candidates = [
		{ id: 1, playing: true, beatSyncEnabled: false },
		{ id: 2, playing: true, beatSyncEnabled: false }
	];
	// Nothing schedules a sync at all.
	assert.equal(audio.syncChangeRequiresReschedule(2, true, false, master), false);
	assert.equal(
		audio.planSeekSync({
			deck: 2,
			transportActive: true,
			beatSyncEnabled: false,
			activeMaster: master,
			beatSyncMax: true,
			candidates
		}).kind,
		'free'
	);
	assert.deepEqual(audio.beatSyncMaxFollowers(master, true, master, candidates), []);
	assert.deepEqual(audio.masterSwitchFollowers(2, activity), []);
	// And crucially: no tempo write on either deck belongs to sync any more, so
	// an in-flight ramp planned before the toggles went off must stop dead.
	assert.equal(audio.syncMayWriteTempo(2, false, master), false);
	assert.equal(audio.syncMayWriteTempo(1, false, master), false);
});

//-----------------------------------------------------------------------------
// the rule is actually consulted at every write path
//-----------------------------------------------------------------------------

test('the re-anchor ramp re-reads ownership before every scheduled step', () => {
	const body = engineBlockAfter(
		'async function _continueTempoRamp(\n' +
			'\tdeck: DeckId,\n' +
			'\tsyncAt: number,\n' +
			'\tremainingSteps: readonly TempoRampStep[],\n' +
			'\tmasterTempoEnabled: boolean,\n' +
			'\tgeneration: number\n' +
			'): Promise<void> {'
	);
	const loopAt = body.indexOf('for (const step of remainingSteps)');
	assert.notEqual(loopAt, -1, 'the ramp must still be a loop over its remaining steps');
	const gateAt = body.indexOf('_syncOwnsFollowerTempo(deck)');
	assert.notEqual(
		gateAt,
		-1,
		'if the ramp tail stops asking who owns this deck tempo then a switched-off ' +
			'deck keeps being written for the rest of the ramp (#1134)'
	);
	assert.ok(
		gateAt > loopAt && gateAt < body.indexOf('await _scheduleDeck'),
		'the gate must sit INSIDE the step loop and before the schedule, not once at entry'
	);
});

test('the ramp gate reads live state rather than a captured copy', () => {
	const body = engineBlockAfter('function _syncOwnsFollowerTempo(deck: DeckId): boolean {');
	assert.match(body, /syncMayWriteTempo\(/);
	assert.match(body, /_masterDeck/);
	assert.match(body, /effectiveBeatSync\(deckStates\[deck\]\)/);
});

test('the sync command refuses a followers list that contains its own master', () => {
	const body = engineBlockAfter(
		'async function _synchronizeFollowers(\n' +
			'\tmaster: DeckId,\n' +
			'\tfollowers: readonly DeckId[],\n' +
			'\toptions: _SyncOptions = {}\n' +
			'): Promise<void> {'
	);
	assert.match(
		body,
		/if \(followers\.includes\(master\)\) \{[\s\S]*throw new RangeError\(/,
		'a master listed among its own followers must throw, not be scheduled'
	);
});

test('the sync command re-reads follower ownership after its awaits, before planning', () => {
	const body = engineBlockAfter(
		'async function _synchronizeFollowers(\n' +
			'\tmaster: DeckId,\n' +
			'\tfollowers: readonly DeckId[],\n' +
			'\toptions: _SyncOptions = {}\n' +
			'): Promise<void> {'
	);
	const ownedAt = body.indexOf('followers.filter((deck) => _syncOwnsFollowerTempo(deck))');
	assert.notEqual(
		ownedAt,
		-1,
		'if the follower set is trusted from the caller then a BEAT SYNC toggled off ' +
			'during this call own awaits is still written (#1134)'
	);
	const planAt = body.indexOf('computeFollowerSyncPlan({');
	assert.ok(ownedAt < planAt, 'ownership must be re-read before any follower is planned');
	assert.match(body, /for \(const deck of owned\)/, 'planning must iterate the owned set');
});

test('pause and beat-sync disable bump re-anchor operation generation', () => {
	const pauseBody = engineBlockAfter('pause(deck: DeckId, pressT0Ms?: number): Promise<void> {');
	assert.match(pauseBody, /_bumpReanchorOperation\(deck\)/);

	const beatSyncBody = engineBlockAfter('setBeatSync(deck: DeckId, enabled: boolean): Promise<void> {');
	const disableAt = beatSyncBody.indexOf('if (!enabled) {');
	assert.notEqual(disableAt, -1);
	assert.match(beatSyncBody.slice(disableAt, disableAt + 400), /_bumpReanchorOperation\(deck\)/);
});

test('the schedule ack path re-checks re-anchor generation after processor.schedule', () => {
	const body = engineBlockAfter(
		'async function _scheduleDeckSerial(\n' +
			'\tdeck: DeckId,\n' +
			'\twhen: number,\n' +
			'\tinputSec: number | ((effectiveWhen: number) => number),\n' +
			'\tactive: boolean,\n' +
			'\ttempoRatio: number | undefined,\n' +
			'\tmasterTempoEnabled: boolean | undefined,\n' +
			'\tloop: LoopState | null | undefined,\n' +
			'\tkeyShiftSemitones: number | undefined,\n' +
			'\tpressT0Ms: number | undefined,\n' +
			'\treanchorGeneration?: number\n' +
			'): Promise<number> {'
	);
	assert.match(
		body,
		/reanchorGeneration !== undefined[\s\S]*_reanchorOperationIsCurrent\(deck, reanchorGeneration\)/
	);
});

test('ramp pending clears only when the owner generation matches', () => {
	const releaseBody = engineBlockAfter('function _releaseReanchorRampOwner(deck: DeckId, generation: number): void {');
	assert.match(releaseBody, /reanchorRampOwnerGeneration === generation/);
});

test('pressing MASTER moves the role before the re-anchor, and puts it back if the lock is refused', () => {
	// Re-pointed after 0a8631e82 "fix: unlock and resume locked paused Beat Sync MASTER
	// (DECKUX-17, #320)": setDeckMaster gained `options?: { lock?: boolean }` and
	// _assignMaster a 'manual' reason. The paused path assigns earlier and returns, so
	// lastIndexOf still lands on the audible path's assignment ahead of the re-anchor.
	const body = engineBlockAfter(
		'async setDeckMaster(deck: DeckId, options?: { lock?: boolean }): Promise<void> {'
	);
	const assignAt = body.lastIndexOf("_assignMaster(deck, 'manual');");
	const syncAt = body.indexOf('await _synchronizeFollowers(deck, followers');
	assert.notEqual(assignAt, -1);
	assert.notEqual(syncAt, -1);
	assert.ok(
		assignAt < syncAt,
		'assigning the master after the re-anchor makes the outgoing master look like ' +
			'the master for the whole operation and withdraws the deck being re-anchored'
	);
	assert.match(body, /_assignMaster\(previousMaster\)/, 'a refused lock must restore the role');
});
