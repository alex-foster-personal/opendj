// requirement: AUDIOLIVE-11
//
// The clocks that keep deck state moving when no animation frame arrives,
// driven here with virtual timers and a virtual audio clock so each case is a
// statement about scheduling, not about how punctual this machine's timers are.
//
// [if] a frame stays pending for a whole interval [then] the backstop runs the
//   loop body from its timer [⛔️ if a hidden tab freezes deck state]
// [if] frames are being delivered [then] the backstop never runs the body
//   [⛔️ if the presentation rate doubles in a visible tab]
// [if] the loop goes idle [then] the backstop holds no timer [⛔️ if it burns
//   a timer for ever with nothing playing]
// [if] page timers are throttled to nothing but the audio clock runs [then] an
//   audio-clock sleep still resolves [⛔️ if a hidden-tab unload waits a minute]
// [if] a presented stop never arrives [then] the wait rejects with a NAMED
//   error at its deadline [⛔️ if it hangs or resolves as though stopped]
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let backstop;
let preset;

before(async () => {
	backstop = await loadTypeScriptModule('src/lib/rb/frame-backstop.ts');
	preset = await loadTypeScriptModule('src/lib/rb/performance-preset-runner.ts');
});

//-----------------------------------------------------------------------------
// virtual clocks
//-----------------------------------------------------------------------------

/** Timers that fire only when the test says so. */
function virtualTimers() {
	const pending = new Map();
	let next = 0;
	return {
		pending,
		set(run, ms) {
			next += 1;
			pending.set(next, { run, ms });
			return next;
		},
		clear(handle) {
			pending.delete(handle);
		},
		/** Fire every timer that is pending right now, once. */
		fire() {
			const due = [...pending.entries()];
			for (const [handle, { run }] of due) {
				pending.delete(handle);
				run();
			}
		}
	};
}

/** An AudioContext clock whose `ended` events the test delivers by advancing it. */
function virtualAudioClock({ state = 'running', outputLagSec = 0 } = {}) {
	const ctx = {
		state,
		currentTime: 0,
		destination: { kind: 'destination' },
		sources: [],
		gains: [],
		getOutputTimestamp: () => ({ contextTime: ctx.currentTime - outputLagSec, performanceTime: 0 }),
		createGain() {
			const gain = { gain: { value: 1 }, connected: null, disconnected: false, connect(to) { gain.connected = to; return to; }, disconnect() { gain.disconnected = true; } };
			ctx.gains.push(gain);
			return gain;
		},
		createConstantSource() {
			const source = {
				onended: null, started: false, stopAt: null, ended: false, connected: null, disconnected: false,
				connect(to) { source.connected = to; return to; },
				disconnect() { source.disconnected = true; },
				start() { source.started = true; },
				stop(when) { source.stopAt = when; }
			};
			ctx.sources.push(source);
			return source;
		},
		/** Move the audio clock and deliver every `ended` that is now due. */
		advanceTo(seconds) {
			ctx.currentTime = seconds;
			for (const source of ctx.sources) {
				if (source.ended || source.stopAt === null || source.stopAt > seconds) continue;
				source.ended = true;
				source.onended?.();
			}
		}
	};
	return ctx;
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

//-----------------------------------------------------------------------------
// the frame backstop
//-----------------------------------------------------------------------------

test('AUDIOLIVE-11: a frame that never fires is run by the backstop, every interval, for as long as it stays pending', () => {
	const timers = virtualTimers();
	let frame = 7;
	let runs = 0;
	const guard = backstop.createFrameBackstop(() => frame, () => { runs += 1; frame += 1; }, 100, timers, () => false);
	guard.arm();
	assert.equal(timers.pending.size, 1, 'a pending frame is watched by exactly one timer');
	assert.equal(runs, 0, 'if the body runs before a whole interval has passed then broken - that is a second clock, not a backstop');
	timers.fire();
	assert.equal(runs, 1, 'if a frame pending for a whole interval is not run then broken - a hidden tab freezes deck state');
	timers.fire();
	timers.fire();
	assert.equal(runs, 3, 'the body keeps running while its re-requested frame keeps stalling');
	assert.equal(timers.pending.size, 1, 'still exactly one timer');
});

test('AUDIOLIVE-11 control: while frames are delivered the backstop never runs the body', () => {
	const timers = virtualTimers();
	let frame = 1;
	let runs = 0;
	const guard = backstop.createFrameBackstop(() => frame, () => { runs += 1; }, 100, timers, () => false);
	guard.arm();
	for (let interval = 0; interval < 5; interval += 1) {
		frame += 6; // six frames were delivered and re-requested since the timer was set
		timers.fire();
	}
	assert.equal(runs, 0, 'if the backstop runs while frames fire then broken - the presentation rate and the audio-Hz meter inflate');
	assert.equal(timers.pending.size, 1, 'and it stays armed for the moment frames stop');
});

test('AUDIOLIVE-11: an idle loop holds no timer, and arming twice holds one', () => {
	const timers = virtualTimers();
	let frame = null;
	let runs = 0;
	const guard = backstop.createFrameBackstop(() => frame, () => { runs += 1; frame = null; }, 100, timers, () => false);
	guard.arm();
	assert.equal(timers.pending.size, 0, 'if an idle loop is watched then broken - a timer burns for ever with nothing playing');
	frame = 3;
	guard.arm();
	guard.arm();
	assert.equal(timers.pending.size, 1, 'if arming twice sets two timers then broken - the body would run twice per interval');
	timers.fire(); // stalled: the body runs and leaves the loop idle
	assert.equal(runs, 1);
	assert.equal(timers.pending.size, 0, 'if the timer survives the loop going idle then broken');
	frame = 4;
	guard.arm();
	guard.cancel();
	assert.equal(timers.pending.size, 0, 'cancel releases the timer');
	guard.arm();
	assert.equal(timers.pending.size, 1, 'and the backstop can be armed again afterwards');
});

//-----------------------------------------------------------------------------
// the audio-clock wake
//-----------------------------------------------------------------------------

test('AUDIOLIVE-11: a hidden page wakes on the audio clock at the instant the change is presented, and not before', () => {
	const timers = virtualTimers();
	const ctx = virtualAudioClock({ outputLagSec: 0.2 });
	ctx.currentTime = 10;
	let runs = 0;
	const guard = backstop.createFrameBackstop(() => 1, () => { runs += 1; }, 100, timers, () => true);
	guard.wake(ctx, 10.05);
	assert.equal(ctx.sources.length, 1, 'if a hidden page schedules no audio-clock wake then broken - only a throttled timer is left');
	const [source] = ctx.sources;
	assert.equal(source.started, true);
	assert.ok(Math.abs(source.stopAt - (10.05 + 0.2 + 0.03)) < 1e-9, `the wake lands at schedule time + output lag + margin, got ${source.stopAt}`);
	assert.equal(ctx.gains[0].gain.value, 0, 'if the wake source is not muted then broken - it would put DC on the master output');
	assert.equal(source.connected, ctx.gains[0], 'the source feeds the mute');
	assert.equal(ctx.gains[0].connected, ctx.destination, 'and the mute feeds the destination so the context renders it');
	ctx.advanceTo(10.2);
	assert.equal(runs, 0, 'if the wake runs before the change reaches the output then broken - it publishes the old state and nothing follows');
	ctx.advanceTo(10.3);
	assert.equal(runs, 1, 'if the audio clock passing the instant does not run the body then broken');
	assert.equal(source.disconnected && ctx.gains[0].disconnected, true, 'the wake nodes are released');
	assert.equal(timers.pending.size, 0, 'the wake used no page timer at all');
});

test('AUDIOLIVE-11 control: a visible page with live frames schedules no wake, and a stopped context is never asked for one', () => {
	const timers = virtualTimers();
	const visible = virtualAudioClock();
	backstop.createFrameBackstop(() => 1, () => {}, 100, timers, () => false).wake(visible, 1);
	assert.equal(visible.sources.length, 0, 'if a visible page allocates wake nodes per transport change then broken - that is garbage on the fader-drag path');
	const suspended = virtualAudioClock({ state: 'suspended' });
	backstop.createFrameBackstop(() => 1, () => {}, 100, timers, () => true).wake(suspended, 1);
	assert.equal(suspended.sources.length, 0, 'a context that is not running has no clock to wake on');
});

test('AUDIOLIVE-11: a page that looks visible but has stalled frames still wakes on the audio clock', () => {
	const timers = virtualTimers();
	const ctx = virtualAudioClock();
	const guard = backstop.createFrameBackstop(() => 1, () => {}, 100, timers, () => false);
	guard.arm();
	timers.fire(); // the same frame was pending a whole interval: stalled
	guard.wake(ctx, 1);
	assert.equal(ctx.sources.length, 1, 'if only document.hidden arms the wake then broken - an occluded window can stall frames without reporting hidden');
});

test('AUDIOLIVE-11: an audio-clock sleep resolves on audio time when page timers never fire, and on the timer when there is no running clock', async () => {
	const throttled = virtualTimers(); // never fired: intensive throttling
	const ctx = virtualAudioClock();
	let woke = false;
	void backstop.audioClockSleep(ctx, 20, throttled).then(() => { woke = true; });
	await flush();
	assert.equal(woke, false, 'control: nothing has elapsed yet');
	ctx.advanceTo(0.02);
	await flush();
	assert.equal(woke, true, 'if the sleep waits for a throttled page timer then broken - a hidden-tab unload takes up to a minute');
	assert.equal(throttled.pending.size, 0, 'the losing timer is cleared');

	for (const clock of [null, virtualAudioClock({ state: 'suspended' })]) {
		const timers = virtualTimers();
		let done = false;
		void backstop.audioClockSleep(clock, 20, timers).then(() => { done = true; });
		await flush();
		assert.equal(done, false);
		timers.fire();
		await flush();
		assert.equal(done, true, 'if a missing or suspended context leaves the sleep with no clock then broken - it never resolves');
	}
});

//-----------------------------------------------------------------------------
// the presented-stop wait
//-----------------------------------------------------------------------------

test('AUDIOLIVE-11: the presented-stop wait re-reads state every step, resolves when it holds, and never touches the frame clock', async () => {
	assert.equal(typeof globalThis.requestAnimationFrame, 'undefined', 'control: this process has no frame clock, so any frame wait would throw');
	let polls = 0;
	let nowMs = 0;
	const sleeps = [];
	await backstop.awaitPresentedStop(
		() => { polls += 1; return polls >= 4; },
		null,
		2000,
		() => nowMs,
		async (_ctx, ms) => { sleeps.push(ms); nowMs += ms; }
	);
	assert.equal(polls, 4, 'the predicate is the publisher: it runs before every decision');
	assert.deepEqual(sleeps, [20, 20, 20], 'paced in 20 ms steps');
});

test('AUDIOLIVE-11: a stop that is never presented rejects with a named error at the deadline', async () => {
	let nowMs = 0;
	let polls = 0;
	await assert.rejects(
		backstop.awaitPresentedStop(() => { polls += 1; return false; }, null, 2000, () => nowMs, async (_ctx, ms) => { nowMs += ms; }),
		(error) => {
			assert.equal(error.name, 'PresentedStopTimeoutError', 'if the timeout is anonymous then broken - the caller cannot tell it from a real fault');
			assert.ok(error instanceof backstop.PresentedStopTimeoutError);
			assert.match(error.message, /2000 ms/);
			return true;
		}
	);
	assert.equal(nowMs, 2000, 'if the wait ends before its deadline then broken - a slow output would be force-unloaded early');
	assert.equal(polls, 101, 'and it kept polling until then');
});

test('AUDIOLIVE-11 control: an already-stopped deck does not sleep at all', async () => {
	let slept = 0;
	await backstop.awaitPresentedStop(() => true, null, 2000, () => 0, async () => { slept += 1; });
	assert.equal(slept, 0, 'if a stopped deck still waits a step then broken - every unload pays 20 ms for nothing');
});

//-----------------------------------------------------------------------------
// frame-paced waits that must still end when hidden
//-----------------------------------------------------------------------------

function installFrames(fires) {
	const requested = [];
	globalThis.requestAnimationFrame = (callback) => {
		requested.push(callback);
		if (fires) setImmediate(() => callback(performance.now()));
		return requested.length;
	};
	return () => { delete globalThis.requestAnimationFrame; };
}

test('AUDIOLIVE-11: frameOrTimeout resolves on the timer when no frame arrives, and on the frame when one does', async () => {
	let restore = installFrames(false);
	try {
		const startedAt = performance.now();
		await backstop.frameOrTimeout(30);
		assert.ok(performance.now() - startedAt >= 25, 'with no frame, the timer is what resolved it');
	} finally {
		restore();
	}
	restore = installFrames(true);
	try {
		const startedAt = performance.now();
		await backstop.frameOrTimeout(5000);
		assert.ok(performance.now() - startedAt < 1000, 'if a delivered frame does not resolve the wait then broken - a visible tab would poll at the timer rate');
	} finally {
		restore();
	}
});

function stoppedState(audible) {
	const deck = { playing: false, audible, transport_pending: false, transport_clock: { presented_revision: 3, desired_revision: 3 } };
	return { master_deck: null, decks: { 1: { ...deck }, 2: { ...deck }, 3: { ...deck }, 4: { ...deck } } };
}

test('AUDIOLIVE-11: a preset stop wait finishes in a hidden tab, and its timeout is still reachable', async () => {
	const restore = installFrames(false);
	try {
		const fixture = { id: 'hidden-tab', decks: [1, 2, 3, 4].map((deck) => ({ deck })) };
		let polls = 0;
		const state = await preset.waitForPerformancePresetStopped(fixture, () => {
			polls += 1;
			return stoppedState(polls < 3);
		}, 5000);
		assert.equal(state.decks[1].audible, false, 'if the wait never polls again without a frame then broken - a preset stop hangs for ever in a hidden tab');
		assert.equal(polls, 3);
		await assert.rejects(
			preset.waitForPerformancePresetStopped(fixture, () => stoppedState(true), 250),
			/did not stop within 250ms/,
			'if the deadline is only read between frames then broken - it is never read in a hidden tab'
		);
	} finally {
		restore();
	}
});
