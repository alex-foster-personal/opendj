/**
 * The boot request window (PERF-R6).
 *
 * What this guards: an A/B bench convicted the startup burst, not steady
 * state. A deck loaded while the page was still booting cost 2.1x more
 * (fetchWall median 3793 -> 8038ms) because nine endpoint families opened at
 * mount, stretching the boot burst from 1187ms to 3579ms against a
 * single-worker daemon and a six-connection pool. The scheduler's whole job
 * is ordering, so ordering is what is asserted here -- with a fake clock, so
 * the suite does not spend BOOT_QUIET_MS of real time proving it.
 *
 * Regression lines:
 *  - if a deferred task runs synchronously then it is still in the boot
 *    burst and the module is doing nothing
 *  - if the queue drains before the idle callback then deferred work can
 *    land in the frame the deck load is decoding in
 *  - if the queue drains while a deck load is in flight then it is competing
 *    for the same connections and this is broken
 *  - if a stuck deck load strands the queue then work is being dropped
 *    silently, which the house rules ban
 *  - if a task deferred after release waits then the scheduler has become a
 *    latency source of its own
 *  - if one throwing task eats the rest of the queue then a single bad
 *    surface can silently disable every other one
 */
import assert from 'node:assert/strict';
import { before, beforeEach, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

/** A hand-driven clock and idle callback. Nothing here runs on its own. */
function makeHost({ idle = true } = {}) {
	const timers = new Map();
	let nextHandle = 1;
	let pendingIdle = null;
	const host = {
		setTimer(run, ms) {
			const handle = nextHandle++;
			timers.set(handle, { run, ms });
			return handle;
		},
		clearTimer(handle) {
			timers.delete(handle);
		},
		whenIdle: idle
			? (run) => {
					pendingIdle = run;
				}
			: null
	};
	return {
		host,
		/** Fire every timer currently armed, once. */
		tickTimers() {
			const due = [...timers.entries()];
			timers.clear();
			for (const [, entry] of due) entry.run();
			return due.length;
		},
		/** Longest delay currently armed, for asserting the quiet period. */
		armedDelays() {
			return [...timers.values()].map((entry) => entry.ms);
		},
		hasIdlePending() {
			return pendingIdle !== null;
		},
		/** Hand the browser's idle frame to the scheduler. */
		fireIdle() {
			const run = pendingIdle;
			pendingIdle = null;
			if (run === null) throw new Error('no idle callback was requested');
			run();
		}
	};
}

/** Arm, wait out the quiet period, go idle: the ordinary release path. */
function releaseNormally(clock) {
	clock.tickTimers();
	clock.fireIdle();
}

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/boot-scheduler.ts');
});

let clock;
let scheduler;
let ran;

beforeEach(() => {
	clock = makeHost();
	scheduler = mod.createBootScheduler(clock.host);
	ran = [];
});

test('a deferred task does not run at mount, and the quiet period is the boot window', () => {
	scheduler.defer('a', () => ran.push('a'));
	scheduler.defer('b', () => ran.push('b'));

	assert.deepEqual(ran, [], 'deferred work must not run inside the mount that queued it');
	assert.deepEqual(
		clock.armedDelays(),
		[mod.BOOT_QUIET_MS],
		'one timer, armed for the quiet period, however many tasks were queued'
	);
});

test('the queue waits for an idle frame, not the timer alone', () => {
	scheduler.defer('a', () => ran.push('a'));

	clock.tickTimers();
	assert.deepEqual(ran, [], 'the quiet period elapsing is not on its own a release');
	assert.ok(clock.hasIdlePending(), 'the scheduler must ask the browser for an idle frame');

	clock.fireIdle();
	assert.deepEqual(ran, ['a']);
});

test('critical calls are not the scheduler business: only deferred work is held', () => {
	// The layout probe / entitlements / setup-status path never touches this
	// module, so "critical first" is structural. What IS asserted is the
	// consequence: work handed over runs strictly after the window closes,
	// and in the order it arrived.
	scheduler.defer('first', () => ran.push('first'));
	scheduler.defer('second', () => ran.push('second'));
	scheduler.defer('third', () => ran.push('third'));

	assert.deepEqual(ran, []);
	releaseNormally(clock);
	assert.deepEqual(ran, ['first', 'second', 'third'], 'arrival order is preserved');
});

test('an in-flight deck load holds the queue back until it settles', () => {
	const settle = scheduler.deckLoadStarted();
	scheduler.defer('a', () => ran.push('a'));

	releaseNormally(clock);
	assert.deepEqual(ran, [], 'a deck load in flight must keep the queue closed');

	// The yield re-checks on its own timer rather than spinning.
	assert.deepEqual(clock.armedDelays(), [mod.DECK_LOAD_YIELD_POLL_MS]);
	clock.tickTimers();
	assert.deepEqual(ran, [], 'still loading, still held');

	settle();
	clock.tickTimers();
	assert.deepEqual(ran, ['a'], 'the queue drains once the decks are free');
});

test('two overlapping deck loads both have to settle', () => {
	const settleOne = scheduler.deckLoadStarted();
	const settleTwo = scheduler.deckLoadStarted();
	scheduler.defer('a', () => ran.push('a'));
	releaseNormally(clock);

	settleOne();
	clock.tickTimers();
	assert.deepEqual(ran, [], 'one of two loads finishing is not the decks being free');

	settleTwo();
	clock.tickTimers();
	assert.deepEqual(ran, ['a']);
});

test('settling twice does not make the counter go negative', () => {
	const settle = scheduler.deckLoadStarted();
	settle();
	settle();
	const stillLoading = scheduler.deckLoadStarted();
	scheduler.defer('a', () => ran.push('a'));

	releaseNormally(clock);
	assert.deepEqual(ran, [], 'a double settle must not cancel out a real load');

	stillLoading();
	clock.tickTimers();
	assert.deepEqual(ran, ['a']);
});

test('a deck load that never settles cannot strand the queue', () => {
	scheduler.deckLoadStarted();
	scheduler.defer('a', () => ran.push('a'));
	releaseNormally(clock);

	const polls = Math.ceil(mod.DECK_LOAD_YIELD_MAX_MS / mod.DECK_LOAD_YIELD_POLL_MS);
	for (let i = 0; i < polls + 1 && ran.length === 0; i += 1) clock.tickTimers();

	assert.deepEqual(ran, ['a'], 'the yield has a ceiling; nothing is ever dropped');
});

// LIBM-138: the All Tracks walk at boot is the page's primary content, and
// every deferred request shares one engine with it.
test('the boot listing walk holds the queue back until it settles', () => {
	const settle = scheduler.listingWalkStarted();
	scheduler.defer('a', () => ran.push('a'));

	releaseNormally(clock);
	assert.deepEqual(ran, [], 'a listing walk in flight must keep the queue closed');
	clock.tickTimers();
	assert.deepEqual(ran, [], 'still walking, still held');

	settle();
	settle();
	clock.tickTimers();
	assert.deepEqual(ran, ['a'], 'the queue drains once the walk is over');
});

test('a listing walk and a deck load both have to settle', () => {
	const settleWalk = scheduler.listingWalkStarted();
	const settleDeck = scheduler.deckLoadStarted();
	scheduler.defer('a', () => ran.push('a'));
	releaseNormally(clock);

	settleWalk();
	clock.tickTimers();
	assert.deepEqual(ran, [], 'the deck is still loading');

	settleDeck();
	clock.tickTimers();
	assert.deepEqual(ran, ['a']);
});

test('a listing walk that never settles cannot strand the queue', () => {
	scheduler.listingWalkStarted();
	scheduler.defer('a', () => ran.push('a'));
	releaseNormally(clock);

	const polls = Math.ceil(mod.DECK_LOAD_YIELD_MAX_MS / mod.DECK_LOAD_YIELD_POLL_MS);
	for (let i = 0; i < polls + 1 && ran.length === 0; i += 1) clock.tickTimers();

	assert.deepEqual(ran, ['a'], 'an abandoned walk releases at the ceiling');
});

test('a task deferred after the window has closed runs immediately', () => {
	scheduler.defer('a', () => ran.push('a'));
	releaseNormally(clock);
	assert.deepEqual(ran, ['a']);

	scheduler.defer('later', () => ran.push('later'));
	assert.deepEqual(ran, ['a', 'later'], 'no second window, no second wait');
});

test('a throwing task is rethrown out of band and the rest of the queue still runs', () => {
	const boom = new Error('the daemon said no');
	scheduler.defer('bad', () => {
		throw boom;
	});
	scheduler.defer('good', () => ran.push('good'));

	releaseNormally(clock);
	assert.deepEqual(ran, ['good'], 'one bad surface must not disable every other one');

	// The failure is queued for a rethrow rather than swallowed, so it stays
	// as loud as the plain `void task()` it replaced.
	assert.throws(() => clock.tickTimers(), /deferred task 'bad' threw/);
});

test('a platform with no idle callback still releases on the timer', () => {
	const bare = makeHost({ idle: false });
	const bareScheduler = mod.createBootScheduler(bare.host);
	bareScheduler.defer('a', () => ran.push('a'));

	bare.tickTimers();
	assert.deepEqual(ran, ['a'], 'requestIdleCallback is an optimization, never a requirement');
});

test('start() arms the window and its teardown disarms it', () => {
	const stop = scheduler.start();
	assert.deepEqual(clock.armedDelays(), [mod.BOOT_QUIET_MS], 'start opens the window');

	stop();
	assert.deepEqual(clock.armedDelays(), [], 'teardown leaves no timer behind');
});
