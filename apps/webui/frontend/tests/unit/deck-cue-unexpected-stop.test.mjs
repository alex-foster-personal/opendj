// requirement: AUDIOLIVE-4806
// [if] CUE is pressed on a playing deck [then] the deck returns to its cue point and stops with no unexpected-stop toast and no fault row, [else stop].
// [if] a deck that is already coming to rest is sent to another position [then] nothing is reported, because no deck went from playing to stopped, [else stop].
// [if] a deck stops with no command behind it [then] the stop is still reported with a toast and a fault row, [else stop].
/**
 * AUDIOLIVE-4806: a stop the DJ commanded is never reported as an unexpected one.
 *
 * WHAT BROKE (installed build 7a441473a on bifrost2, Thu 1 Oct 2026,
 * D:\odj-evidence\run-20261001-cue-toast): a real click on CUE while deck 1
 * played raised the error toast "Deck 1 stopped at 1:33, before the track's
 * listed end (3:24). The audio file may be cut short." and wrote the ring row
 * `audio-unexpected-pause cause=source-ended-early`. A real click on PAUSE in
 * the same session raised nothing.
 *
 * WHY. The reporter (issue #2153) treats every stop that reaches
 * `_scheduleDeck` as unexpected unless the caller says it is a command.
 * `pause()` says so; `pressCue()` did not. Two more callers had the same
 * shape: Crash Rescue's Undo (`rescueStopAllTogether`), and any re-schedule of
 * a deck that is ALREADY stopping (CUE or a seek pressed before the deck has
 * come to rest), which is not a playing-to-stopped transition at all.
 *
 * This file drives the REAL engine through real-engine-rig.mjs.
 *
 * MUTATION CHECK, both directions: see the table in the commit that added
 * this file. Every control below names the mutation it exists to catch.
 */
import assert from 'node:assert/strict';
import { afterEach, before, beforeEach, describe, it } from 'node:test';

import { atRest, audiblyPlaying, bundleRealEngine, startRealEngine } from './real-engine-rig.mjs';

// The engine reads the page URL for ?extroute=; these tests run as a page with no query.
globalThis.location ??= { search: '' };

const SID_A = 'a'.repeat(40);
const SID_B = 'b'.repeat(40);

/** @type {string} */
let bundleText;
/** @type {Awaited<ReturnType<typeof startRealEngine>>} */
let rig;

const deck = () => rig.deck(1);
/** What the DJ would see and what the diagnostics ring would hold. */
const reported = () => ({
	faultRows: rig.unexpectedPauseRows().map((row) => row.message),
	errorToasts: rig.errorToasts()
});
const NOTHING = { faultRows: [], errorToasts: [] };

/** Load deck 1 with no stem work (the installed-build check ran on a track with none) and play it. */
async function loadAndPlay() {
	await rig.engine.load(1, SID_A, { stems: false });
	await rig.engine.play(1);
	await rig.stepUntil(() => audiblyPlaying(deck()), 'audibly playing');
	// Half a second in: far enough from both ends of the 4 s track that a stop
	// here is "mid-track" to the reporter, as a stop at 1:33 of 3:24 was.
	await rig.stepFrames(30);
	assert.ok(deck().position_ms > 300, 'the playhead never moved: nothing below would be a mid-track stop');
	assert.deepEqual(reported(), NOTHING, 'something was reported before the action under test');
}

describe('a stop the DJ commanded is not reported as an unexpected stop (AUDIOLIVE-4806)', { concurrency: false }, () => {
	before(async () => {
		bundleText = await bundleRealEngine();
	});

	beforeEach(async () => {
		rig = await startRealEngine(bundleText, {
			tracks: { [SID_A]: { title: 'Track A' }, [SID_B]: { title: 'Track B' } }
		});
	});

	afterEach(async () => {
		await rig.stop();
		assert.deepEqual(
			rig.tickFailures().map((row) => row.message),
			[],
			'the real presentation frame threw under this harness, so nothing above measured the engine'
		);
	});

	it('CUE on a playing deck: the deck stops at its cue point and nothing is reported', async () => {
		await loadAndPlay();
		await rig.engine.pressCue(1);
		await rig.stepUntil(() => atRest(deck()), 'at rest after CUE');
		assert.equal(deck().position_ms, 0, 'CUE with no cue point set returns to the start of the track');
		assert.deepEqual(reported(), NOTHING, 'CUE on a playing deck was reported as an unexpected stop');
	});

	it('control: PAUSE on a playing deck is not reported', async () => {
		await loadAndPlay();
		await rig.engine.pause(1);
		await rig.stepUntil(() => atRest(deck()), 'at rest after PAUSE');
		assert.deepEqual(reported(), NOTHING);
	});

	it('CUE pressed again before the deck has come to rest: nothing is reported', async () => {
		await loadAndPlay();
		// PAUSE stores the memory cue, so the second CUE below is a seek to it.
		await rig.engine.pause(1);
		await rig.stepUntil(() => atRest(deck()), 'at rest after PAUSE');
		assert.notEqual(deck().cue_ms, null, 'PAUSE must store the memory cue');
		await rig.engine.play(1);
		await rig.stepUntil(() => audiblyPlaying(deck()), 'audibly playing again');
		await rig.engine.pressCue(1);
		assert.equal(atRest(deck()), false, 'the deck came to rest with no frame stepped: the second press would test nothing');
		// The second press finds the deck stopping, not stopped: the engine
		// re-schedules the stop at the cue point. No deck went from playing to
		// stopped here, so there is nothing to report.
		await rig.engine.pressCue(1);
		await rig.stepUntil(() => atRest(deck()), 'at rest after the second CUE');
		assert.deepEqual(reported(), NOTHING, 'a CUE pressed while the deck was already stopping was reported as an unexpected stop');
	});

	it('a seek sent while PAUSE is still landing: nothing is reported', async () => {
		await loadAndPlay();
		await rig.engine.pause(1);
		assert.equal(atRest(deck()), false, 'the deck came to rest with no frame stepped: the seek would test nothing');
		await rig.engine.quantizedSeek(1, 2000);
		await rig.stepUntil(() => atRest(deck()), 'at rest after the seek');
		assert.deepEqual(reported(), NOTHING, 'a seek on a deck that was already stopping was reported as an unexpected stop');
	});

	it('Crash Rescue Undo stops the rescued deck and nothing is reported', async () => {
		await rig.engine.load(1, SID_A, { stems: false });
		// 4c5540e6a5 (RESCUE-06): the resume names the master deck it must include.
		await rig.engine.rescueResumeTogether([{ deck: 1, positionSec: 1 }], 1);
		await rig.stepUntil(() => audiblyPlaying(deck()), 'the rescued deck audibly playing');
		await rig.stepFrames(20);
		await rig.engine.rescueStopAllTogether([1]);
		await rig.stepUntil(() => atRest(deck()), 'at rest after Undo');
		assert.deepEqual(reported(), NOTHING, 'Crash Rescue Undo was reported as an unexpected stop');
	});

	it('control: a hot cue jump on a playing deck keeps it playing and reports nothing', async () => {
		await loadAndPlay();
		await rig.engine.quantizedSeek(1, 2000);
		await rig.stepFrames(30);
		assert.equal(audiblyPlaying(deck()), true, 'a jump on a playing deck must not stop it');
		assert.ok(deck().position_ms >= 2000, 'the jump did not land');
		assert.deepEqual(reported(), NOTHING);
	});

	it('control: a tempo move and a key nudge on a playing deck keep it playing and report nothing', async () => {
		await loadAndPlay();
		await rig.engine.setTempoRatio(1, 1.02);
		await rig.engine.nudgeKey(1, 1);
		await rig.stepFrames(30);
		assert.equal(audiblyPlaying(deck()), true, 'a tempo or key change on a playing deck must not stop it');
		assert.deepEqual(reported(), NOTHING);
	});

	it('control: the natural end of the track is not reported', async () => {
		await loadAndPlay();
		rig.advanceClock(5);
		await rig.stepUntil(() => atRest(deck()), 'at rest after the natural end');
		assert.deepEqual(reported(), NOTHING);
	});

	it('control: a load onto a playing deck is refused, the deck keeps playing and nothing is reported', async () => {
		await loadAndPlay();
		await assert.rejects(rig.engine.load(1, SID_B, { stems: false }), 'a load must not replace a playing deck');
		await rig.stepFrames(10);
		assert.equal(deck().stable_id, SID_A);
		assert.equal(audiblyPlaying(deck()), true, 'the refused load stopped the deck');
		assert.deepEqual(rig.unexpectedPauseRows(), [], 'a refused load was reported as an unexpected stop');
	});

	it('control: a deck whose processor fails while playing IS reported', async () => {
		// Catches the overshoot: a fix that quiets the reporter itself, not the
		// one caller that forgot to say it was a command.
		await loadAndPlay();
		const [mix] = rig.processors();
		mix.options.onProcessorError(new Error('worklet render failed (test)'));
		const { faultRows, errorToasts } = reported();
		assert.equal(faultRows.length, 1, 'a real unexpected stop wrote no fault row');
		assert.match(faultRows[0], /cause=worklet-error deck=1/);
		assert.deepEqual(errorToasts, ['Deck 1 stopped unexpectedly. The audio processor failed.']);
	});

	it('control: after a CUE press a stop with no command behind it is still attributed to nobody, and reported', async () => {
		// Catches the overshoot: the command tag outliving the press that set it
		// (or becoming the default), which would silence every later stop.
		await loadAndPlay();
		await rig.engine.pressCue(1);
		await rig.stepUntil(() => atRest(deck()), 'at rest after CUE');
		assert.equal(rig.entry.readPauseOrigin(), 'other', 'the command tag outlived the CUE press');
		// 2cba7940ef: the falling edge reports only for a deck the DJ had playing.
		rig.entry.notePlayingFallingEdge({
			was_active: true,
			origin: rig.entry.readPauseOrigin(),
			deck: 1,
			position_ms: 1500,
			duration_ms: deck().duration_ms,
			metadata_duration_ms: null,
			processor_error: null,
			context_state: 'running'
		});
		const { faultRows, errorToasts } = reported();
		assert.equal(faultRows.length, 1, 'exactly one fault row is owed: the uncommanded stop, and nothing for the CUE before it');
		assert.match(faultRows[0], /cause=source-ended-early deck=1 position_ms=1500/);
		assert.deepEqual(errorToasts, ['Deck 1 stopped at 0:01, before the decoded audio ends (0:04). The audio file may be cut short.']);
	});
});
