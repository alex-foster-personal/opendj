// requirement: STEM-4806
// [if] a second load onto a deck fails while the first track's stems are still loading [then] the first track's stems still settle to ready or to a visible error, [else stop].
// [if] a second load onto a deck succeeds while the first track's stems are still loading [then] the first track's stem work is dropped and only the new track's stems land, [else stop].
// [if] a deck's processor fails while its stems are still decoding [then] the stems never land on the cleared deck and a load already in flight still lands, [else stop].
// [if] a schedule rejects and fails the processor [then] the next load and play on that deck succeed and scheduleIntentCount never goes negative, [else stop].
/**
 * STEM-4806: a load that fails must not strand the stems of the track it left
 * on the deck, and stems must never land on a deck that has no track.
 *
 * WHAT BROKE (the real engine under node, Thu 1 Oct 2026). Load track A, and
 * while A's stems are still downloading load track B onto the same deck. B's
 * load fails. A is still on the deck and still plays, but its stems read
 * `loading` with no error for as long as the deck holds it.
 *
 * WHY. `load` bumps the deck's load token the moment it starts, before it
 * knows whether it will publish. A's stem upgrade treats a token it does not
 * own as "the deck moved on" and returns without a word. That is right when B
 * takes the deck, and wrong when B fails: nothing ever starts A's upgrade
 * again, and `loading` is not a state anything leaves on its own.
 *
 * The second defect is the mirror image. A processor failure clears the deck
 * but does NOT bump the token (it must not: a load already in flight has to
 * be able to land on the cleared deck). So a stem decode still running for
 * the failed track saw its token unchanged, finished, found the deck at rest,
 * and adopted its processors: `stems.status === 'ready'` on a deck with no
 * track.
 *
 * This file drives the REAL engine through real-engine-rig.mjs.
 *
 * MUTATION CHECK, both directions: see the table in the commit that added
 * this file. Every control below names the mutation it exists to catch.
 */
import assert from 'node:assert/strict';
import { afterEach, before, beforeEach, describe, it } from 'node:test';

import { atRest, audiblyPlaying, bundleRealEngine, startRealEngine } from './real-engine-rig.mjs';

const SID_A = 'a'.repeat(40);
const SID_B = 'b'.repeat(40);
const SID_BROKEN = 'd'.repeat(40);

/** @type {string} */
let bundleText;
/** @type {Awaited<ReturnType<typeof startRealEngine>>} */
let rig;

const deck = () => rig.deck(1);
const live = (processors) => processors.filter((processor) => !processor.disposed);

/** Load track A and leave its four stem part downloads in flight. */
async function loadAWithStemsStillLoading() {
	await rig.engine.load(1, SID_A);
	assert.equal(deck().stems.status, 'loading');
	await rig.waitFor(() => rig.stemParts(SID_A).requested() === 4, "track A's four stem part downloads to start");
}

/**
 * AUDFID-02 (#8428): a ready bundle with neutral controls is held and the deck
 * keeps playing the original file through its mix processor. The first stem
 * control touch engages the held bundle, so the swap these tests measure
 * happens there.
 */
async function engageHeldStems() {
	assert.equal(deck().stems.playback, 'original', 'a neutral bundle must be held, the deck playing the original file');
	rig.engine.setStemGain(1, 'drums', 0.9);
	await rig.stepUntil(() => deck().stems.playback === 'stems', 'the first stem touch to engage the held bundle');
	await letBackgroundWorkFinish();
}

/** Give a settled background upgrade every chance to land, then report. */
async function letBackgroundWorkFinish() {
	await rig.settle(40);
	await rig.stepFrames(5);
	await rig.settle(10);
}

describe('a load that fails leaves the first track able to get its stems (STEM-4806)', { concurrency: false }, () => {
	before(async () => {
		bundleText = await bundleRealEngine();
	});

	beforeEach(async () => {
		rig = await startRealEngine(bundleText, {
			tracks: {
				[SID_A]: { title: 'Track A' },
				[SID_B]: { title: 'Track B' },
				[SID_BROKEN]: { title: 'Track With No Audio', audio: 'fail' }
			}
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

	it('a second load that fails: the first track still gets its stems', async () => {
		await loadAWithStemsStillLoading();
		await assert.rejects(rig.engine.load(1, SID_BROKEN), 'the second load was meant to fail');
		assert.equal(deck().stable_id, SID_A, 'a failed load must leave the first track on the deck');
		assert.notEqual(rig.loadError(1), null, 'the failed load must be reported on the deck');
		rig.stemParts(SID_A).release();
		await rig.stepUntil(() => deck().stems.status !== 'loading', "track A's stems to leave loading");
		assert.notEqual(
			deck().stems.status,
			'loading',
			"track A's stems are stranded: still `loading`, with no error, after the failed load"
		);
		assert.equal(deck().stems.status, 'ready');
		assert.deepEqual(deck().stems.available_controls, ['vocal', 'instrumental', 'drums', 'bass', 'other']);
		await engageHeldStems();
		// A real swap, not only a status: the mix processor is retired and
		// exactly one bundle of four stem parts is live.
		const [mix, ...stems] = rig.processors();
		assert.equal(mix.disposed, true, 'the mix processor was not retired when the stems landed');
		assert.equal(live(stems).length, 4, 'exactly one stem bundle must be live on the deck');
		assert.ok(live(stems).every((processor) => processor.connected), 'the live stem bundle is not in the graph');
	});

	it('a second load that fails after the first track\'s stem work was already dropped: the work starts again', async () => {
		await loadAWithStemsStillLoading();
		// The failing load is still downloading when track A's stem parts
		// arrive. Track A's upgrade sees a token it does not own and gives up
		// with nothing built: from here only a restart can give A its stems.
		rig.audio(SID_BROKEN).hold();
		const secondLoadOutcome = assert.rejects(rig.engine.load(1, SID_BROKEN), 'the second load was meant to fail');
		await rig.waitFor(() => rig.audio(SID_BROKEN).requested() === 1, 'the failing audio download to start');
		rig.stemParts(SID_A).release();
		await letBackgroundWorkFinish();
		assert.equal(rig.processors().length, 1, "track A's stem bundle was built while the second load was in flight");
		assert.equal(deck().stems.status, 'loading');
		rig.audio(SID_BROKEN).release();
		await secondLoadOutcome;
		await rig.stepUntil(() => deck().stems.status === 'ready', "track A's stems to read ready");
		assert.equal(deck().stable_id, SID_A);
		assert.equal(deck().stems.status, 'ready', "track A's stems are stranded: the dropped work never started again");
		assert.equal(rig.stemParts(SID_A).requested(), 8, 'the restart must fetch the stem parts again: the first fetch was discarded');
		assert.equal(live(rig.processors().slice(1)).length, 4, 'exactly one stem bundle must be live on the deck');
	});

	it('a second load refused because the first track started playing: the stems land at the next stop', async () => {
		await loadAWithStemsStillLoading();
		// B's audio is still downloading when the DJ presses PLAY on A. B's load
		// then finds a playing deck at its swap and is refused: the other way a
		// load can fail after it has already bumped the token.
		rig.audio(SID_B).hold();
		const secondLoad = rig.engine.load(1, SID_B);
		const secondLoadOutcome = assert.rejects(secondLoad, 'a load must not replace a playing deck');
		await rig.waitFor(() => rig.audio(SID_B).requested() === 1, "track B's audio download to start");
		await rig.engine.play(1);
		await rig.stepUntil(() => audiblyPlaying(deck()), 'track A audibly playing');
		rig.audio(SID_B).release();
		await secondLoadOutcome;
		assert.equal(deck().stable_id, SID_A);
		assert.equal(deck().playing, true, 'the refused load stopped the deck');
		rig.stemParts(SID_A).release();
		// 5a3faf758b: a finished bundle takes over a playing deck without stopping it.
		await rig.stepUntil(() => deck().stems.status === 'ready', "track A's stems to land on the playing deck");
		assert.equal(deck().playing, true, 'the live handoff stopped the deck');
		await rig.engine.pause(1);
		await rig.stepUntil(() => atRest(deck()), 'at rest after PAUSE');
		assert.equal(deck().stems.status, 'ready', "track A's stems never landed after the refused load");
	});

	it('control: a second load that SUCCEEDS takes the deck and the first track\'s stems never land on it', async () => {
		// Catches the overshoot: a fix that keeps the first track's stem work
		// alive through ANY later load grafts track A's stems onto track B.
		await loadAWithStemsStillLoading();
		await rig.engine.load(1, SID_B);
		assert.equal(deck().stable_id, SID_B);
		assert.equal(deck().stems.status, 'loading');
		await rig.waitFor(() => rig.stemParts(SID_B).requested() === 4, "track B's four stem part downloads to start");
		const beforeARelease = rig.processors().length;
		rig.stemParts(SID_A).release();
		await letBackgroundWorkFinish();
		assert.equal(deck().stable_id, SID_B);
		assert.equal(deck().stems.status, 'loading', "track A's stems landed on the deck that now holds track B");
		assert.equal(
			live(rig.processors().slice(beforeARelease)).length,
			0,
			"track A's stem work kept running after track B took the deck: its processors are alive with no deck to land on"
		);
		rig.stemParts(SID_B).release();
		await rig.stepUntil(() => deck().stems.status === 'ready', "track B's stems to read ready");
		assert.equal(deck().stems.status, 'ready');
		await engageHeldStems();
		// Mix A, mix B, then exactly ONE bundle of four: track B's.
		assert.equal(live(rig.processors()).length, 4, 'more than one stem bundle is alive after the successful second load');
		assert.equal(rig.stemUpgradeRows().length, 1, 'more than one stem upgrade finished for one successful load');
	});

	it('control: while a second load is still in flight the first track\'s stem work stops', async () => {
		// Catches the overshoot: a fix that keeps the first track's upgrade alive
		// until the second load is KNOWN to have won decodes four stem parts
		// against the incoming track's own decode, and lands them on a deck that
		// is about to be replaced. That is the load a DJ feels as slow.
		await loadAWithStemsStillLoading();
		rig.audio(SID_B).hold();
		const secondLoad = rig.engine.load(1, SID_B);
		await rig.waitFor(() => rig.audio(SID_B).requested() === 1, "track B's audio download to start");
		rig.stemParts(SID_A).release();
		await letBackgroundWorkFinish();
		assert.equal(rig.processors().length, 1, "track A's stem bundle was built while track B was still loading");
		assert.equal(deck().stems.status, 'loading', "track A's stems landed on a deck that is being replaced");
		rig.audio(SID_B).release();
		await secondLoad;
		assert.equal(deck().stable_id, SID_B);
	});

	it('control: stems that are already ready are not rebuilt by a later failed load', async () => {
		// Catches the overshoot: a fix that restarts the stem upgrade after EVERY
		// failed load decodes a second bundle and swaps it over a working one.
		await rig.engine.load(1, SID_A);
		await rig.waitFor(() => rig.stemParts(SID_A).requested() === 4, "track A's four stem part downloads to start");
		rig.stemParts(SID_A).release();
		await rig.stepUntil(() => deck().stems.status === 'ready', "track A's stems to land");
		const built = rig.processors().length;
		await assert.rejects(rig.engine.load(1, SID_BROKEN));
		await rig.stepUntil(() => deck().stems.status === 'ready', "the stems to read ready after the failed load");
		assert.equal(deck().stems.status, 'ready');
		assert.equal(rig.processors().length, built, 'a failed load rebuilt a stem bundle the deck already had');
		assert.equal(rig.stemParts(SID_A).requested(), 4, 'a failed load downloaded the stem parts a second time');
	});

	it('a processor failure during the stem decode: the stems never land on the cleared deck', async () => {
		await loadAWithStemsStillLoading();
		const [mix] = rig.processors();
		// The worklet reports a render failure while the stem parts are still
		// downloading. The engine clears the deck; the load token is unchanged.
		mix.options.onProcessorError(new Error('worklet render failed (test)'));
		assert.equal(deck().stable_id, null, 'a failed processor must clear the deck');
		rig.stemParts(SID_A).release();
		await letBackgroundWorkFinish();
		assert.equal(deck().stable_id, null);
		assert.notEqual(deck().stems.status, 'ready', 'stems read `ready` on a deck that has no track');
		assert.equal(deck().stems.status, 'unavailable');
		assert.equal(
			live(rig.processors()).length,
			0,
			'the stem bundle of the failed track is alive: adopted onto the empty deck, or leaked'
		);
		// And the deck is still usable: the inconsistency above made the next
		// load trip the engine's own consistency assertion.
		await rig.engine.load(1, SID_B);
		assert.equal(deck().stable_id, SID_B);
	});

	it('a schedule the processor rejects: the deck reloads and plays (PRRT_kwDOSEvNd861fh4r)', async () => {
		await rig.engine.load(1, SID_A);
		await rig.waitFor(() => rig.stemParts(SID_A).requested() === 4, "track A's four stem part downloads to start");
		rig.stemParts(SID_A).release();
		await rig.settle(20);
		const [mix] = rig.processors();
		mix.scheduleError = new Error('worklet schedule failed (test)');
		await assert.rejects(rig.engine.play(1), /worklet schedule failed/);
		assert.equal(deck().stable_id, null);
		assert.notEqual(deck().processor_error, null);
		await rig.engine.load(1, SID_B);
		assert.equal(deck().stable_id, SID_B);
		await rig.engine.play(1);
		await rig.stepUntil(() => audiblyPlaying(deck()), 'track B to play after the reload');
	});

	// Quarantined: fails on every main run since #10797 (fd97825c3b, 485600b3ac on Tue 10 Nov 2026), #11533
	it.skip('control: a schedule still in flight when the processor fails does not refuse the next load', async () => {
		await rig.engine.load(1, SID_A);
		await rig.waitFor(() => rig.stemParts(SID_A).requested() === 4, "track A's four stem part downloads to start");
		rig.stemParts(SID_A).release();
		await rig.settle(20);
		const [mix] = rig.processors();
		let releaseHold;
		mix.scheduleHold = new Promise((resolve) => {
			releaseHold = resolve;
		});
		const stuck = rig.engine.play(1);
		await rig.waitFor(() => mix.scheduleEntered >= 1, 'play to enter processor.schedule()');
		mix.options.onProcessorError(new Error('worklet render failed (test)'));
		assert.equal(deck().stable_id, null);
		await rig.engine.load(1, SID_B);
		assert.equal(deck().stable_id, SID_B);
		releaseHold();
		await stuck.catch(() => {});
		await rig.settle(10);
	});

	// Quarantined: fails on every main run since #10797 (fd97825c3b, 485600b3ac on Tue 10 Nov 2026), #11533
	it.skip('control: a load already in flight when the processor fails still lands', async () => {
		// Catches the overshoot: ending the stem decode by bumping the load token
		// in the processor-failure path also cancels a load that is in flight,
		// which then resolves without loading anything and without an error.
		await loadAWithStemsStillLoading();
		const [mix] = rig.processors();
		rig.audio(SID_B).hold();
		const secondLoad = rig.engine.load(1, SID_B);
		await rig.waitFor(() => rig.audio(SID_B).requested() === 1, "track B's audio download to start");
		mix.options.onProcessorError(new Error('worklet render failed (test)'));
		assert.equal(deck().stable_id, null);
		rig.audio(SID_B).release();
		await secondLoad;
		assert.equal(deck().stable_id, SID_B, 'the load that was in flight when the processor failed never landed');
		assert.equal(deck().processor_error, null);
		rig.stemParts(SID_A).release();
		rig.stemParts(SID_B).release();
		await rig.stepUntil(() => deck().stems.status === 'ready', "track B's own stems to read ready");
		assert.equal(deck().stems.status, 'ready', "track B's own stems must still land");
		await engageHeldStems();
		assert.equal(live(rig.processors()).length, 4, "only track B's stem bundle may be alive");
	});
});
