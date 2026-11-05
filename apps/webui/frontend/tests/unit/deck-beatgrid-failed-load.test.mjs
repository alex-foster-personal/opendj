// requirement: PARITY-4832
// [if] a second load onto a deck fails while the gridless first track's beatgrid upgrade is unsettled [then] the first track still gets its grid, [else stop].
// [if] a second load onto a deck succeeds while the first track's beatgrid upgrade is unsettled [then] the first track's grid never lands on the deck that now holds the second track, [else stop].
// [if] the first track's beatgrid upgrade already settled (grid landed, or no analysis) before a second load failed [then] the failed load does not ask again, [else stop].
/**
 * PARITY-4832: a load that fails must not strand the beatgrid upgrade of the
 * track it leaves on the deck. STEM-4806's sibling, for the deferred grid.
 *
 * WHAT BROKE (the real engine under node, Thu 1 Oct 2026). Load a locally
 * imported track A, whose /anlz has no grid, so `load` starts the PARITY-10
 * fallback upgrade after the deck can play. While A's /beatgrid-fallback is
 * still in flight, load track B onto the same deck, and B's load fails. A is
 * still on the deck and still plays, but it stays gridless, with no error and
 * no toast, until it is loaded again: no beat ticks, no quantize, no beat
 * loops, nothing for Beat Sync to lock to.
 *
 * WHY. `load` bumps the deck's load token the moment it starts, before it
 * knows whether it will publish. A's upgrade (`upgradeDeckBeatgrid`, keyed on
 * `() => token !== rt.loadToken`) reads a token it does not own as "the deck
 * moved on" and returns without settling. Right when B takes the deck, wrong
 * when B fails: nothing started A's upgrade again. STEM-4806 fixed this for the
 * stem upgrade only.
 *
 * This file drives the REAL engine through real-engine-rig.mjs, with tracks
 * whose /anlz is gridless and whose /rb-meta and /beatgrid-fallback answers
 * are the verified backend captures.
 *
 * MUTATION CHECK, both directions: see the table in the commit that added
 * this file. Every control below names the mutation it exists to catch.
 */
import assert from 'node:assert/strict';
import { afterEach, before, beforeEach, describe, it } from 'node:test';

import { FALLBACK_BEATS, audiblyPlaying, bundleRealEngine, startRealEngine } from './real-engine-rig.mjs';

const SID_A = 'a'.repeat(40);
const SID_B = 'b'.repeat(40);
const SID_UNANALYZED = 'c'.repeat(40);
const SID_BROKEN = 'd'.repeat(40);
const SID_UNANALYZED_B = 'e'.repeat(40);

/** @type {string} */
let bundleText;
/** @type {Awaited<ReturnType<typeof startRealEngine>>} */
let rig;

const deck = () => rig.deck(1);
const beats = () => deck().anlz?.beatgrid.beats ?? null;
const beatgridToasts = () => rig.errorToasts().filter((message) => /beatgrid/i.test(message));

/** Load gridless track A and leave its /beatgrid-fallback request in flight. */
async function loadAWithFallbackInFlight() {
	rig.beatgridFallback(SID_A).hold();
	await rig.engine.load(1, SID_A);
	await rig.waitFor(() => rig.beatgridFallback(SID_A).requested() >= 1, "track A's /beatgrid-fallback request to start");
	await rig.settle(10);
	assert.equal(rig.beatgridFallback(SID_A).requested(), 1, 'one load that succeeds must start exactly one beatgrid upgrade');
	assert.deepEqual(beats(), [], 'track A must load gridless: the fallback grid is what this file is about');
}

/** Give a settled background upgrade every chance to land, then report. */
async function letBackgroundWorkFinish() {
	await rig.settle(40);
	await rig.stepFrames(5);
	await rig.settle(10);
}

describe('a load that fails leaves the first track able to get its beatgrid (PARITY-4832)', { concurrency: false }, () => {
	before(async () => {
		bundleText = await bundleRealEngine();
	});

	beforeEach(async () => {
		rig = await startRealEngine(bundleText, {
			tracks: {
				[SID_A]: { title: 'Gridless Track A', grid: 'none' },
				[SID_B]: { title: 'Track B' },
				[SID_UNANALYZED]: { title: 'Gridless, Not Analyzed', grid: 'none', fallback: 'missing' },
				[SID_UNANALYZED_B]: { title: 'Gridless B, Not Analyzed', grid: 'none', fallback: 'missing' },
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

	it('control: a gridless track gets its fallback grid when nothing else happens', async () => {
		// Positive control for the rig itself: without it, "the grid never
		// landed" below could be a harness that cannot land a grid at all.
		await loadAWithFallbackInFlight();
		rig.beatgridFallback(SID_A).release();
		await letBackgroundWorkFinish();
		assert.deepEqual(beats(), FALLBACK_BEATS);
		assert.deepEqual(beatgridToasts(), []);
	});

	it('a second load that fails: the gridless first track still gets its grid', async () => {
		await loadAWithFallbackInFlight();
		await assert.rejects(rig.engine.load(1, SID_BROKEN), 'the second load was meant to fail');
		assert.equal(deck().stable_id, SID_A, 'a failed load must leave the first track on the deck');
		assert.notEqual(rig.loadError(1), null, 'the failed load must be reported on the deck');
		rig.beatgridFallback(SID_A).release();
		await letBackgroundWorkFinish();
		assert.deepEqual(beats(), FALLBACK_BEATS, "track A's beatgrid upgrade is stranded: still gridless, with no error, after the failed load");
		assert.deepEqual(beatgridToasts(), []);
	});

	it("a second load that fails after the first track's upgrade was already dropped: the upgrade starts again", async () => {
		await loadAWithFallbackInFlight();
		// The failing load is still downloading when track A's fallback grid
		// arrives. Track A's upgrade sees a token it does not own and gives up
		// with nothing landed: from here only a restart can give A its grid.
		rig.audio(SID_BROKEN).hold();
		const secondLoadOutcome = assert.rejects(rig.engine.load(1, SID_BROKEN), 'the second load was meant to fail');
		await rig.waitFor(() => rig.audio(SID_BROKEN).requested() === 1, 'the failing audio download to start');
		rig.beatgridFallback(SID_A).release();
		await letBackgroundWorkFinish();
		assert.deepEqual(beats(), [], "track A's grid landed while the second load was in flight");
		rig.audio(SID_BROKEN).release();
		await secondLoadOutcome;
		await letBackgroundWorkFinish();
		assert.equal(deck().stable_id, SID_A);
		assert.deepEqual(beats(), FALLBACK_BEATS, "track A's beatgrid upgrade is stranded: the dropped upgrade never started again");
		assert.equal(rig.beatgridFallback(SID_A).requested(), 2, 'the restart must ask again: the first answer was discarded');
		assert.deepEqual(beatgridToasts(), []);
	});

	it('a second load refused because the first track started playing: the grid still lands', async () => {
		await loadAWithFallbackInFlight();
		// B's audio is still downloading when the DJ presses PLAY on A. B's load
		// then finds a playing deck at its swap and is refused: the other way a
		// load can fail after it has already bumped the token.
		rig.audio(SID_B).hold();
		const secondLoadOutcome = assert.rejects(rig.engine.load(1, SID_B), 'a load must not replace a playing deck');
		await rig.waitFor(() => rig.audio(SID_B).requested() === 1, "track B's audio download to start");
		await rig.engine.play(1);
		await rig.stepUntil(() => audiblyPlaying(deck()), 'track A audibly playing');
		rig.audio(SID_B).release();
		await secondLoadOutcome;
		assert.equal(deck().stable_id, SID_A);
		assert.equal(deck().playing, true, 'the refused load stopped the deck');
		rig.beatgridFallback(SID_A).release();
		await letBackgroundWorkFinish();
		assert.deepEqual(beats(), FALLBACK_BEATS, "track A's beatgrid upgrade is stranded after the refused load");
		assert.equal(deck().playing, true, 'landing the grid stopped the deck');
		assert.deepEqual(beatgridToasts(), []);
	});

	it("control: a second load that SUCCEEDS takes the deck and the first track's grid never lands on it", async () => {
		// Catches the overshoot: a fix that keeps the first track's upgrade alive
		// through ANY later load, or restarts it after one that succeeds, grafts
		// track A's grid onto the next track. That track is gridless and has no
		// analysis of its own, so any grid on the deck afterwards is A's.
		await loadAWithFallbackInFlight();
		await rig.engine.load(1, SID_UNANALYZED_B);
		assert.equal(deck().stable_id, SID_UNANALYZED_B);
		await letBackgroundWorkFinish();
		rig.beatgridFallback(SID_A).release();
		await letBackgroundWorkFinish();
		assert.equal(deck().stable_id, SID_UNANALYZED_B);
		assert.deepEqual(beats(), [], "track A's grid landed on the deck that now holds another track");
		assert.equal(rig.beatgridFallback(SID_A).requested(), 1, "track A's upgrade was started again after a load that succeeded");
		assert.equal(
			rig.beatgridFallback(SID_UNANALYZED_B).rbMetaRequested(),
			1,
			'one successful load must start exactly one beatgrid upgrade for the track it loaded'
		);
		assert.deepEqual(beatgridToasts(), []);
	});

	it('control: a swap that fails after a newer load has started leaves the deck to that load', async () => {
		// Catches dropping the swap exit's `token === rt.loadToken` guard. The
		// grid upgrade, unlike the stem upgrade, goes stale ONLY by token, so a
		// restart taken while a newer load already owns the token stays live
		// after that load wins, and lands track A's grid on the newer track.
		await loadAWithFallbackInFlight();
		// B's swap fails on a deck that is not playing (its processor cannot
		// connect), and the failure's own cleanup is held open.
		const failingSwap = rig.failNextSwap();
		const secondLoadOutcome = assert.rejects(rig.engine.load(1, SID_B), /connect failed/);
		await rig.waitFor(() => failingSwap.disposing(), "track B's failed swap to start disposing its processor");
		// A third load starts inside that window and takes the token.
		rig.audio(SID_UNANALYZED_B).hold();
		const thirdLoad = rig.engine.load(1, SID_UNANALYZED_B);
		await rig.waitFor(() => rig.audio(SID_UNANALYZED_B).requested() === 1, "the third load's audio download to start");
		failingSwap.releaseDispose();
		await secondLoadOutcome;
		rig.audio(SID_UNANALYZED_B).release();
		await thirdLoad;
		assert.equal(deck().stable_id, SID_UNANALYZED_B);
		await letBackgroundWorkFinish();
		rig.beatgridFallback(SID_A).release();
		await letBackgroundWorkFinish();
		assert.deepEqual(beats(), [], "track A's grid landed on the track that took the deck after the failed swap");
		assert.equal(rig.beatgridFallback(SID_A).requested(), 1, "track A's upgrade was restarted by a load that no longer owned the deck");
	});

	it('control: an upgrade that already settled without a grid is not asked again by a later failed load', async () => {
		// Catches the overshoot: a restart after EVERY failed load re-asks a
		// question the backend already answered ("no analysis yet") and settles
		// Beat Sync for it a second time.
		await rig.engine.load(1, SID_UNANALYZED);
		await letBackgroundWorkFinish();
		assert.equal(rig.beatgridFallback(SID_UNANALYZED).requested(), 1, 'the first upgrade never reached /beatgrid-fallback');
		assert.deepEqual(beats(), []);
		await assert.rejects(rig.engine.load(1, SID_BROKEN));
		await letBackgroundWorkFinish();
		assert.equal(deck().stable_id, SID_UNANALYZED);
		assert.equal(rig.beatgridFallback(SID_UNANALYZED).rbMetaRequested(), 1, 'a failed load asked /rb-meta again for a settled answer');
		assert.equal(rig.beatgridFallback(SID_UNANALYZED).requested(), 1, 'a failed load asked /beatgrid-fallback again for a settled answer');
	});

	it('control: a grid that already landed is not fetched again by a later failed load', async () => {
		await rig.engine.load(1, SID_A);
		await letBackgroundWorkFinish();
		assert.deepEqual(beats(), FALLBACK_BEATS);
		await assert.rejects(rig.engine.load(1, SID_BROKEN));
		await letBackgroundWorkFinish();
		assert.deepEqual(beats(), FALLBACK_BEATS);
		assert.equal(rig.beatgridFallback(SID_A).rbMetaRequested(), 1, 'a failed load re-ran the upgrade for a deck that already has its grid');
	});

	it('control: a processor failure while the fallback is in flight: the grid never lands on the cleared deck', async () => {
		// The mirror of STEM-4806's second defect: a processor failure clears the
		// deck without bumping the token, so A's upgrade is not stale when its
		// answer arrives. The publish must still find no track to land on.
		await loadAWithFallbackInFlight();
		const [mix] = rig.processors();
		mix.options.onProcessorError(new Error('worklet render failed (test)'));
		assert.equal(deck().stable_id, null, 'a failed processor must clear the deck');
		rig.beatgridFallback(SID_A).release();
		await letBackgroundWorkFinish();
		assert.equal(deck().stable_id, null);
		assert.equal(deck().anlz, null, "track A's grid landed on a deck with no track");
	});
});
