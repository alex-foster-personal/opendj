import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { engineBlockAfter, readFrontendSource } from './engine-source.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * P0 (audio-never-cuts-out-under-thrash, defect D1): a context that stops
 * running while a deck is playing must be NOTICED.
 *
 * Wed 2 Sep 2026, 12:58 CEST: the output device was lost and re-found
 * underneath a running AudioContext while the machine was in swap thrash. The
 * app went silent for ~24 minutes and displayed nothing. Audio came back only
 * because the maintainer changed a macOS system setting by hand.
 *
 * The engine has exactly one `statechange` listener and its entire body is
 * `if (state === 'running') stampContextDeviceFloors(...)`. `suspended`,
 * `interrupted` (the state WebKit uses for precisely this class of event) and
 * `closed` fall through it in silence. `_resumeContext()` exists but only ever
 * runs from an explicit `play()`, so a context that dies mid-playback is
 * resumed by nothing.
 *
 * DEVICE-AGNOSTIC BY CONSTRUCTION: the context here is a plain object with a
 * settable `state`. No Bluetooth, no HAL buffer, no product name - "the state
 * left `running` while a deck was playing" is the entire input.
 *
 * Regression lines:
 * - if a non-running context while playing shows the operator nothing then the
 *   room finds out before the screen does, which is the whole P0
 * - if the drop leaves no perf row carrying context_running: 0 then there is
 *   no after-the-fact record of when audio actually stopped
 * - if recovery is a single resume() with no retry then one failed attempt
 *   against a device that is still settling ends recovery permanently
 * - if a suspended context with nothing playing toasts anyway then the autoplay
 *   policy's normal suspended-at-startup state becomes a false alarm
 */

const WATCHDOG_MODULE = 'src/lib/rb/audio-context-watchdog.ts';
const NON_RUNNING_STATES = ['suspended', 'interrupted', 'closed'];

let watchdog = null;
let loadError = null;

before(async () => {
	try {
		watchdog = await loadTypeScriptModule(WATCHDOG_MODULE);
	} catch (error) {
		loadError = error;
	}
});

/**
 * The module under test, or a failure that names the missing contract rather
 * than a bundler stack trace.
 */
function _watchdog() {
	assert.equal(
		loadError,
		null,
		`if ${WATCHDOG_MODULE} does not exist then broken - nothing in the app owns a ` +
			'non-running AudioContext, so a context that stops running mid-set is handled ' +
			`by no code at all (bundler said: ${loadError === null ? '' : String(loadError)})`
	);
	return watchdog;
}

//-----------------------------------------------------------------------------
// the fakes
//-----------------------------------------------------------------------------

/**
 * An AudioContext that is only a state machine and a resume button.
 *
 * `resume()` records WHEN it was called against the injected clock, which is
 * what lets the backoff be asserted as arithmetic rather than as elapsed real
 * time on a loaded laptop.
 */
function fakeAudioContext({ resumeSucceeds = false } = {}) {
	const listeners = new Set();
	const ctx = {
		state: 'running',
		sampleRate: 44100,
		baseLatency: 128 / 44100,
		outputLatency: 0.032,
		resumeAtMs: [],
		addEventListener: (type, handler) => {
			if (type === 'statechange') listeners.add(handler);
		},
		removeEventListener: (type, handler) => {
			if (type === 'statechange') listeners.delete(handler);
		},
		resume: async () => {
			ctx.resumeAtMs.push(ctx.__nowMs());
			if (!resumeSucceeds) throw new Error('device is not available');
			ctx.state = 'running';
			for (const handler of listeners) handler();
		},
		/** Drive it: flip the state and fire the event the browser would fire. */
		__enter: (state) => {
			ctx.state = state;
			for (const handler of listeners) handler();
		},
		__nowMs: () => 0
	};
	return ctx;
}

/**
 * Effects plus a virtual clock.
 *
 * `sleep` does not sleep: it advances the clock and yields. A backoff test that
 * waited out real timers would be measuring this machine's spare CPU, and this
 * machine is deliberately kept in thrash.
 */
function fakeEffects() {
	let nowMs = 0;
	const toasts = [];
	const perf = [];
	return {
		nowMs: () => nowMs,
		toasts,
		perf,
		effects: {
			pushToast: (message, kind = 'info') => toasts.push({ message, kind }),
			recordPerfTiming: (kind, stages) => perf.push({ kind, stages }),
			sleep: async (ms) => {
				nowMs += ms;
				await Promise.resolve();
			},
			noteUnexpectedPause: (state) => {
				perf.push({ kind: 'audio-unexpected-pause', state });
			}
		}
	};
}

/** Let the watchdog's async recovery loop run to completion. */
async function settle() {
	for (let i = 0; i < 200; i += 1) await Promise.resolve();
}

async function _waitForWatchdogGiveUp({ harness, perf, ctx, deadlineMs = 5000 }) {
	const deadline = Date.now() + deadlineMs;
	const giveUpToast = () =>
		harness.toasts.some(
			(toast) => toast.kind === 'error' && toast.message.includes('did not come back')
		);
	const deadPerf = () =>
		perf.some((row) => row.kind === 'audio-output-dead' && row.severity === 'error');
	for (;;) {
		if (ctx.resumeAtMs.length >= 2 && giveUpToast() && deadPerf()) return;
		if (Date.now() > deadline) {
			throw new Error(
				'timed out waiting for watchdog give-up after hung resumes ' +
					`(resumeAtMs=${ctx.resumeAtMs.length}, giveUpToast=${giveUpToast()}, deadPerf=${deadPerf()})`
			);
		}
		await settle();
		await new Promise((resolve) => setTimeout(resolve, 5));
	}
}

async function driveDrop(state, { playing = true, resumeSucceeds = false } = {}) {
	const mod = _watchdog();
	const harness = fakeEffects();
	const ctx = fakeAudioContext({ resumeSucceeds });
	ctx.__nowMs = harness.nowMs;
	const detach = mod.installAudioContextWatchdog(ctx, harness.effects, () => playing);
	ctx.__enter(state);
	await settle();
	if (typeof detach === 'function') detach();
	return { ...harness, ctx };
}

//-----------------------------------------------------------------------------
// (i) the operator is told
//-----------------------------------------------------------------------------

test('every non-running context state records an unexpected pause while a deck is playing', async () => {
	for (const state of NON_RUNNING_STATES) {
		const run = await driveDrop(state);
		const rows = run.perf.filter((row) => row.kind === 'audio-unexpected-pause');
		assert.ok(
			rows.length >= 1,
			`if entering '${state}' while playing records no audio-unexpected-pause row then broken`
		);
		assert.equal(rows[0].state, state);
	}
});

test('CONTROL: non-running context with nothing playing does not record unexpected pause', async () => {
	const run = await driveDrop('suspended', { playing: false });
	const rows = run.perf.filter((row) => row.kind === 'audio-unexpected-pause');
	assert.equal(rows.length, 0);
});

test('every non-running context state raises an error toast while a deck is playing', async () => {
	for (const state of NON_RUNNING_STATES) {
		const run = await driveDrop(state);
		const errors = run.toasts.filter((toast) => toast.kind === 'error');
		assert.ok(
			errors.length >= 1,
			`if the AudioContext enters '${state}' while a deck is playing and no error toast ` +
				'appears then broken - this is exactly the Wed 2 Sep 2026 incident, where the ' +
				'app was silent for ~24 minutes and displayed nothing at all'
		);
		assert.match(
			errors[0].message,
			new RegExp(state, 'i'),
			`if the toast does not name the state ('${state}') then broken - "something went ` +
				'wrong with audio" is not actionable mid-set'
		);
	}
});

//-----------------------------------------------------------------------------
// (ii) it is written down
//-----------------------------------------------------------------------------

test('every non-running context state records a perf row carrying context_running: 0', async () => {
	for (const state of NON_RUNNING_STATES) {
		const run = await driveDrop(state);
		const rows = run.perf.filter((row) => row.kind === 'audio-context');
		assert.ok(
			rows.length >= 1,
			`if entering '${state}' writes no audio-context perf row then broken - there is ` +
				'then no durable record of WHEN audio stopped, only the operator memory of it'
		);
		assert.ok(
			rows.some((row) => row.stages.context_running === 0),
			`if no audio-context row for '${state}' carries context_running: 0 then broken - ` +
				'the field that distinguishes a running context from a dead one is the one ' +
				'field the row must have'
		);
	}
});

//-----------------------------------------------------------------------------
// (iii) it tries to come back, more than once
//-----------------------------------------------------------------------------

test('recovery retries resume() with a backoff, not one attempt and a shrug', async () => {
	for (const state of NON_RUNNING_STATES) {
		// A `closed` context can never resume and will need the graph rebuilt;
		// asserting the ATTEMPT is still right, because it proves the drop was
		// acted on rather than ignored. What must not happen is silence.
		const run = await driveDrop(state);
		assert.ok(
			run.ctx.resumeAtMs.length >= 2,
			`if '${state}' produces ${run.ctx.resumeAtMs.length} resume attempt(s) then ` +
				'broken - a device that is mid-reconnect (the Wed 2 Sep 2026 device was lost ' +
				'at 12:58:47 and found again at 12:58:52, five seconds later) needs the retry ' +
				'to still be trying when it comes back'
		);
		const gaps = run.ctx.resumeAtMs.slice(1).map((at, i) => at - run.ctx.resumeAtMs[i]);
		assert.ok(
			gaps.every((gap, i) => i === 0 || gap >= gaps[i - 1]),
			`if the retries for '${state}' are evenly spaced rather than backing off then ` +
				`broken - got gaps ${JSON.stringify(gaps)}, and a fixed-interval retry against ` +
				'a device that is gone is a main-thread busy loop during a set'
		);
	}
});

test('the backoff is bounded, so a permanently dead device is not an infinite loop', async () => {
	const mod = _watchdog();
	assert.ok(
		Array.isArray(mod.CONTEXT_RESUME_BACKOFF_MS) && mod.CONTEXT_RESUME_BACKOFF_MS.length > 1,
		'if the resume backoff is not a declared, finite schedule then broken - an unbounded ' +
			'retry on the main thread is a second way to lose the audio it is trying to save'
	);
	const run = await driveDrop('suspended');
	assert.ok(
		run.ctx.resumeAtMs.length <= mod.CONTEXT_RESUME_BACKOFF_MS.length,
		'if recovery retries past the declared schedule then broken - the schedule is not ' +
			'then the schedule'
	);
});

//-----------------------------------------------------------------------------
// the controls: it must not cry wolf, and it must stop when it worked
//-----------------------------------------------------------------------------

test('CONTROL: a suspended context with nothing playing is not an alarm', async () => {
	// Every context starts suspended until a gesture resumes it. If that toasts,
	// the operator learns to ignore the channel before the real drop arrives.
	const run = await driveDrop('suspended', { playing: false });
	assert.equal(
		run.toasts.filter((toast) => toast.kind === 'error').length,
		0,
		'if a suspended context with no deck playing raises an error toast then broken - ' +
			'that is the normal autoplay-policy state at every page load'
	);
});

test('CONTROL: a resume that works stops the retries and says so', async () => {
	const run = await driveDrop('suspended', { resumeSucceeds: true });
	assert.equal(
		run.ctx.resumeAtMs.length,
		1,
		'if recovery keeps retrying after the context is running again then broken'
	);
	assert.ok(
		run.perf.some((row) => row.kind === 'audio-context' && row.stages.context_running === 1),
		'if a recovered context writes no context_running: 1 row then broken - the log then ' +
			'shows audio dying and never shows it coming back, so the outage has no end'
	);
});

//-----------------------------------------------------------------------------
// wiring: the engine must actually be the thing that installs it
//-----------------------------------------------------------------------------

test('the watchdog module owns every non-running state, not just one of them', () => {
	// A source guard rather than another behavioural test: the three states are
	// already driven above, and what this pins is that no future edit narrows the
	// set back down to whichever one somebody happened to reproduce.
	const source = readFrontendSource(WATCHDOG_MODULE);
	for (const state of NON_RUNNING_STATES) {
		assert.ok(
			source.includes(`'${state}'`),
			`if the watchdog stops naming '${state}' then broken - that is the state WebKit ` +
				'uses when a device is taken away, and it fell through in silence for ~24 ' +
				'minutes on Wed 2 Sep 2026'
		);
	}
});

test('the graph arms the watchdog, so it is not merely written', () => {
	const graph = engineBlockAfter('function _ensureGraph(): AudioContext {');
	assert.ok(
		graph.includes('armAudioContextWatchdog('),
		'if the graph is built without arming the context watchdog then broken - the module ' +
			'exists and nothing calls it, which catches exactly as many incidents as not ' +
			'having written it'
	);
	assert.ok(
		!graph.includes("addEventListener('statechange'"),
		'if the engine keeps its own statechange listener alongside the watchdog then broken ' +
			'- two owners of one event is how the running-only branch survived in the first ' +
			'place; the re-stamp now lives inside the watchdog arming'
	);
});

//-----------------------------------------------------------------------------
// (v) the bounded schedule RUNS OUT, so something has to be able to re-arm it
//
// Wed 9 Sep 2026. The schedule above spans 9,050 ms and `statechange` fires on
// TRANSITIONS, so a context that goes `interrupted` and STAYS `interrupted`
// fires exactly once. Six attempts against a device that is still gone, one
// toast, and then nothing in the app ever asks again: the liveness poll returns
// `idle` for any context that is not `running`, and `_resumeContext()` runs
// only from an explicit `play()`. Everything that really takes a device away -
// a Bluetooth re-pair, a phone call, a screen lock, a backgrounded WKWebView -
// outlasts nine seconds comfortably.
//
// The fix must NOT be a longer or unbounded schedule: the bound is a tested
// decision two tests up, and an unbounded main-thread resume loop is a second
// way to lose the audio it is trying to save. So the same bounded schedule is
// RE-ARMED from outside, on the edges that carry new information.
//-----------------------------------------------------------------------------

/** Drive a drop and KEEP the watchdog attached, so opportunities can be fired. */
async function driveDropAttached(state, { playing = true } = {}) {
	const mod = _watchdog();
	const harness = fakeEffects();
	const ctx = fakeAudioContext({ resumeSucceeds: false });
	ctx.__nowMs = harness.nowMs;
	const detach = mod.installAudioContextWatchdog(ctx, harness.effects, () => playing);
	ctx.__enter(state);
	await settle();
	return { ...harness, ctx, detach, mod };
}

test('a recovery opportunity re-runs the bounded schedule once it has run out', async () => {
	const run = await driveDropAttached('interrupted');
	const afterFirstArming = run.ctx.resumeAtMs.length;
	assert.equal(
		afterFirstArming,
		run.mod.CONTEXT_RESUME_BACKOFF_MS.length,
		'the first arming must exhaust the schedule, or this test is not measuring what ' +
			'happens AFTER it runs out'
	);

	// The device comes back 40s later, long after the 9,050ms schedule gave up.
	// Nothing about the context changed, so `statechange` does not fire: this is
	// the ONLY news the app gets.
	run.mod.noteRecoveryOpportunity('the output device list changed');
	await settle();

	assert.ok(
		run.ctx.resumeAtMs.length > afterFirstArming,
		'if a recovery opportunity after the schedule ran out produces no further resume ' +
			'attempt then broken - the context stays interrupted forever and the operator ' +
			'has silence with no way back except a reload'
	);
	run.detach();
});

test('a re-armed recovery that works leaves the context running', async () => {
	const mod = _watchdog();
	const harness = fakeEffects();
	// Dead through the whole first arming, alive by the time the window is
	// focused again: the actual shape of a Bluetooth device coming back.
	let deviceIsBack = false;
	const ctx = fakeAudioContext();
	ctx.__nowMs = harness.nowMs;
	const listeners = [];
	ctx.addEventListener = (type, handler) => {
		if (type === 'statechange') listeners.push(handler);
	};
	ctx.removeEventListener = () => {};
	ctx.resume = async () => {
		ctx.resumeAtMs.push(ctx.__nowMs());
		if (!deviceIsBack) throw new Error('device is not available');
		ctx.state = 'running';
		for (const handler of listeners) handler();
	};
	const detach = mod.installAudioContextWatchdog(ctx, harness.effects, () => true);
	ctx.state = 'interrupted';
	for (const handler of listeners) handler();
	await settle();
	assert.equal(ctx.state, 'interrupted', 'the first arming must fail, or there is nothing to re-arm');

	deviceIsBack = true;
	mod.noteRecoveryOpportunity('the window became visible');
	await settle();

	assert.equal(
		ctx.state,
		'running',
		'if the re-armed recovery does not bring a context back once the device really is ' +
			'available then broken - the retry exists to be the thing that finally works'
	);
	detach();
});

test('each opportunity is still BOUNDED by the declared schedule', async () => {
	// The overshoot this guards: "retry until it works" satisfies the report
	// above perfectly and reinstates the unbounded main-thread loop the bound
	// exists to prevent. One opportunity must buy exactly one schedule.
	const run = await driveDropAttached('suspended');
	const perArming = run.mod.CONTEXT_RESUME_BACKOFF_MS.length;
	run.mod.noteRecoveryOpportunity('the output device list changed');
	await settle();
	assert.equal(
		run.ctx.resumeAtMs.length,
		perArming * 2,
		`if one opportunity produces anything other than one more bounded schedule then ` +
			`broken - got ${run.ctx.resumeAtMs.length} attempts across two armings of ${perArming}`
	);
	run.detach();
});

test('CONTROL: an opportunity against a RUNNING context does nothing', async () => {
	const mod = _watchdog();
	const harness = fakeEffects();
	const ctx = fakeAudioContext();
	ctx.__nowMs = harness.nowMs;
	const detach = mod.installAudioContextWatchdog(ctx, harness.effects, () => true);
	// Never dropped: state is `running` throughout.
	mod.noteRecoveryOpportunity('the output device list changed');
	await settle();
	assert.equal(
		ctx.resumeAtMs.length,
		0,
		'if a healthy running context is resumed on every device change then broken - ' +
			'devicechange fires on every headphone plug and this would cycle a working graph'
	);
	detach();
});

test('CONTROL: an opportunity with nothing playing does nothing', async () => {
	const run = await driveDropAttached('suspended', { playing: false });
	run.mod.noteRecoveryOpportunity('the window became visible');
	await settle();
	assert.equal(
		run.ctx.resumeAtMs.length,
		0,
		'if an idle suspended context is resumed on every tab focus then broken - that is ' +
			'the normal autoplay-policy state at every page load, on every visibility change'
	);
	run.detach();
});

test('detach drops the recovery listener, so a retired context is never resumed', async () => {
	// The exact leak the rebind module documents for its own stall listener:
	// `noteRecoveryOpportunity` fans out to EVERY registered listener, so one
	// left behind by a route unmount answers each later device change by
	// resuming a context that has been closed.
	const run = await driveDropAttached('interrupted');
	const beforeDetach = run.ctx.resumeAtMs.length;
	run.detach();
	run.mod.noteRecoveryOpportunity('the output device list changed');
	await settle();
	assert.equal(
		run.ctx.resumeAtMs.length,
		beforeDetach,
		'if a detached watchdog still answers recovery opportunities then broken - every ' +
			'route visit leaks one more listener holding one more dead context'
	);
});

test('a re-armed recovery that also fails names the real state in its toast', async () => {
	// The wart this pins: `recover` hands its argument to `_describe`, which
	// matches the three state names EXACTLY and otherwise falls through to
	// "an unexpected state (...)". Passing a composed sentence such as
	// "interrupted (retrying after the window became visible)" puts the whole
	// string inside that fallback, so the operator's one actionable toast reads
	// as gibberish precisely when audio is dead.
	const run = await driveDropAttached('interrupted');
	run.mod.noteRecoveryOpportunity('the output device list changed');
	await settle();
	const errors = run.toasts.filter((toast) => toast.kind === 'error');
	assert.ok(errors.length >= 2, 'the re-armed schedule must also give up loudly');
	const last = errors[errors.length - 1].message;
	assert.ok(
		last.includes('the audio device was taken away (interrupted)'),
		`if the give-up toast does not describe the real state then broken - got ${JSON.stringify(last)}`
	);
	assert.ok(
		!last.includes('unexpected state'),
		`if the state name fell through _describe's fallback then broken - got ${JSON.stringify(last)}`
	);
	run.detach();
});

//-----------------------------------------------------------------------------
// (vi) an opportunity that lands MID-schedule is held, never dropped
//
// Sol P1 BLOCKING on PR #1644, and it is the same defect #1619 fixed one module
// over. The window between the final failed `resume()` and `recovering`
// clearing is exactly when a device coming back announces itself, and a
// `devicechange` landing in it is the only new signal there will ever be: no
// further `statechange` follows a context that never changed state.
//-----------------------------------------------------------------------------

/**
 * Drive a drop and fire a recovery opportunity from INSIDE the schedule, on the
 * Nth resume attempt, so the arrival really is mid-flight rather than after.
 */
async function driveDropWithMidScheduleOpportunity({ fireOnAttempt, deviceReturns }) {
	const mod = _watchdog();
	const harness = fakeEffects();
	let deviceIsBack = false;
	let deviceReturnsAfterThisAttempt = false;
	const listeners = [];
	const ctx = fakeAudioContext();
	ctx.__nowMs = harness.nowMs;
	ctx.addEventListener = (type, handler) => {
		if (type === 'statechange') listeners.push(handler);
	};
	ctx.removeEventListener = () => {};
	ctx.resume = async () => {
		ctx.resumeAtMs.push(ctx.__nowMs());
		if (ctx.resumeAtMs.length === fireOnAttempt) {
			mod.noteRecoveryOpportunity('the output device list changed');
			// The device comes back AFTER this attempt has already failed, never
			// during it. Otherwise the firing attempt succeeds by itself and the
			// test passes without the held opportunity doing anything - measured:
			// it stayed green under the drop-the-opportunity mutation.
			if (deviceReturns) deviceReturnsAfterThisAttempt = true;
		}
		if (!deviceIsBack) {
			if (deviceReturnsAfterThisAttempt) {
				deviceReturnsAfterThisAttempt = false;
				deviceIsBack = true;
			}
			throw new Error('device is not available');
		}
		ctx.state = 'running';
		for (const handler of listeners) handler();
	};
	const detach = mod.installAudioContextWatchdog(ctx, harness.effects, () => true);
	ctx.state = 'interrupted';
	for (const handler of listeners) handler();
	await settle();
	return { ...harness, ctx, detach, mod };
}

test('an opportunity arriving mid-schedule is HELD and re-runs after it', async () => {
	const perArming = _watchdog().CONTEXT_RESUME_BACKOFF_MS.length;
	// Fired on the LAST attempt: the narrowest version of the window, and the
	// one Sol named.
	const run = await driveDropWithMidScheduleOpportunity({
		fireOnAttempt: perArming,
		deviceReturns: false
	});
	assert.ok(
		run.ctx.resumeAtMs.length > perArming,
		'if an opportunity that lands inside the schedule is dropped then broken - that is ' +
			`exactly the #1619 defect, one module over. Got ${run.ctx.resumeAtMs.length} ` +
			`attempt(s) against one arming of ${perArming}`
	);
	run.detach();
});

test('a mid-schedule opportunity whose device really is back recovers the context', async () => {
	const perArming = _watchdog().CONTEXT_RESUME_BACKOFF_MS.length;
	const run = await driveDropWithMidScheduleOpportunity({
		fireOnAttempt: perArming,
		deviceReturns: true
	});
	assert.equal(
		run.ctx.state,
		'running',
		'if the held opportunity does not bring the context back then broken - holding it ' +
			'is only worth doing because the retry is the thing that finally works'
	);
	run.detach();
});

test('CONTROL: a held opportunity is NOT re-run once the context came back on its own', async () => {
	// The overshoot: re-running the held edge unconditionally would cycle a graph
	// that is already healthy, on every device change that overlapped a recovery.
	const mod = _watchdog();
	const harness = fakeEffects();
	const listeners = [];
	const ctx = fakeAudioContext();
	ctx.__nowMs = harness.nowMs;
	ctx.addEventListener = (type, handler) => {
		if (type === 'statechange') listeners.push(handler);
	};
	ctx.removeEventListener = () => {};
	ctx.resume = async () => {
		ctx.resumeAtMs.push(ctx.__nowMs());
		// An edge lands mid-schedule, and THIS attempt is the one that works.
		mod.noteRecoveryOpportunity('the output device list changed');
		ctx.state = 'running';
		for (const handler of listeners) handler();
	};
	const detach = mod.installAudioContextWatchdog(ctx, harness.effects, () => true);
	ctx.state = 'interrupted';
	for (const handler of listeners) handler();
	await settle();
	assert.equal(
		ctx.resumeAtMs.length,
		1,
		`if a held opportunity re-runs against an already-running context then broken - got ` +
			`${ctx.resumeAtMs.length} resume attempt(s) where the first one succeeded`
	);
	detach();
});

test('detach cancels a held mid-schedule opportunity', async () => {
	const perArming = _watchdog().CONTEXT_RESUME_BACKOFF_MS.length;
	const mod = _watchdog();
	const harness = fakeEffects();
	const listeners = [];
	const ctx = fakeAudioContext();
	ctx.__nowMs = harness.nowMs;
	ctx.addEventListener = (type, handler) => {
		if (type === 'statechange') listeners.push(handler);
	};
	ctx.removeEventListener = () => {};
	let detach = () => {};
	ctx.resume = async () => {
		ctx.resumeAtMs.push(ctx.__nowMs());
		if (ctx.resumeAtMs.length === perArming) {
			mod.noteRecoveryOpportunity('the output device list changed');
			// The route unmounts while the edge is held, before the schedule exits.
			detach();
		}
		throw new Error('device is not available');
	};
	detach = mod.installAudioContextWatchdog(ctx, harness.effects, () => true);
	ctx.state = 'interrupted';
	for (const handler of listeners) handler();
	await settle();
	assert.equal(
		ctx.resumeAtMs.length,
		perArming,
		'if teardown leaves a held opportunity armed then broken - the running schedule re-arms ' +
			`it against a context the route already closed. Got ${ctx.resumeAtMs.length} attempts`
	);
});

//-----------------------------------------------------------------------------
// (vi.b) THE PLAYING GATE APPLIES ON LANDING, NEVER AGAIN ON RUNNING
//
// Codex thread 3973882771 (P1 BLOCKING) on PR #1644, and the THIRD instance of
// one defect class. #1619 fixed it twice in `audio-output-rebind.ts` -- a held
// request re-gated on `isAnyDeckPlaying()` when it ran, and a gate sampled
// after the debounce rather than at the trigger. The held edge here was re-
// gated the same way in the schedule's own `finally`.
//
// WHY IT MATTERS RATHER THAN BEING TIDY: an operator whose audio has stopped
// PAUSES. That is the normal reaction, and it used to be the thing that
// discarded the one edge saying the device came back. `statechange` fires on
// TRANSITIONS, so a context that stayed `interrupted` will never announce
// itself again, and until this PR pressing play could not repair it either --
// `_resumeContext()` resumed a `suspended` context only. Silence was permanent
// until an unrelated external edge or a reload.
//
// THE OVERSHOOT IS THE OPPOSITE ERROR and is what the CONTROL below pins: an
// opportunity that was NEVER accepted while playing must still be DROPPED.
// `devicechange` fires on every headphone plug, and holding those would arm a
// schedule against an idle graph.
//
// Regression lines:
// - if an edge accepted while a deck was playing is discarded because the
//   operator paused before the schedule finished then broken - that is #1619's
//   defect a third time, and nothing else will ask again
// - if an edge that arrived with nothing playing is held anyway then broken -
//   the gate on LANDING is the rate limit, and relaxing it arms a schedule per
//   headphone plug
//-----------------------------------------------------------------------------

/**
 * Drive a dead device through a whole schedule, firing a recovery opportunity
 * during the LAST attempt (the narrowest version of the window) and optionally
 * pausing playback in the same instant.
 *
 * `pauseWhenOpportunityLands` is the finding: accepted while playing, then
 * paused before the schedule exits. `pauseBeforeOpportunityLands` is the
 * control: the pause happens FIRST, so the opportunity is never accepted at
 * all and must be dropped by the entry gate.
 */
async function driveDropWithPauseAroundHeldEdge({
	pauseWhenOpportunityLands = false,
	pauseBeforeOpportunityLands = false
} = {}) {
	const mod = _watchdog();
	const perArming = mod.CONTEXT_RESUME_BACKOFF_MS.length;
	const harness = fakeEffects();
	const listeners = [];
	const ctx = fakeAudioContext();
	ctx.__nowMs = harness.nowMs;
	ctx.addEventListener = (type, handler) => {
		if (type === 'statechange') listeners.push(handler);
	};
	ctx.removeEventListener = () => {};
	let playing = true;
	ctx.resume = async () => {
		ctx.resumeAtMs.push(ctx.__nowMs());
		// Halfway through: the operator gives up and pauses BEFORE any edge
		// arrives, so the edge that follows is never accepted.
		if (pauseBeforeOpportunityLands && ctx.resumeAtMs.length === Math.ceil(perArming / 2)) {
			playing = false;
		}
		if (ctx.resumeAtMs.length === perArming) {
			mod.noteRecoveryOpportunity('the output device list changed');
			// Accepted a moment ago while a deck was playing; the operator pauses
			// before the schedule that is holding it has finished.
			if (pauseWhenOpportunityLands) playing = false;
		}
		// The device stays gone for this whole run, so any further attempt can
		// only come from the held edge and never from a resume that worked.
		throw new Error('device is not available');
	};
	const detach = mod.installAudioContextWatchdog(ctx, harness.effects, () => playing);
	ctx.state = 'interrupted';
	for (const handler of listeners) handler();
	await settle();
	detach();
	return { ...harness, ctx, perArming };
}

test('an opportunity accepted while playing survives the operator pausing mid-schedule', async () => {
	const run = await driveDropWithPauseAroundHeldEdge({ pauseWhenOpportunityLands: true });
	assert.ok(
		run.ctx.resumeAtMs.length > run.perArming,
		'if pausing after an edge was accepted discards it then broken - the operator ' +
			'pausing is the NORMAL reaction to audio stopping, the context stays ' +
			'interrupted with no further statechange coming, and the device-list edge was ' +
			`the only news there was ever going to be. Got ${run.ctx.resumeAtMs.length} ` +
			`attempt(s) against one arming of ${run.perArming}`
	);
});

test('CONTROL: an opportunity that was never accepted while playing is still dropped', async () => {
	// The overshoot: preserving edges regardless of whether they were ever
	// accepted would arm a bounded schedule on every headphone plug against a
	// graph nobody is listening to.
	const run = await driveDropWithPauseAroundHeldEdge({ pauseBeforeOpportunityLands: true });
	assert.equal(
		run.ctx.resumeAtMs.length,
		run.perArming,
		'if an edge that arrived with nothing playing is held anyway then broken - the ' +
			'playing gate on LANDING is what keeps this bounded, and only an edge that ' +
			`passed it may survive later. Got ${run.ctx.resumeAtMs.length} attempt(s) ` +
			`against one arming of ${run.perArming}`
	);
});

//-----------------------------------------------------------------------------
// (vi.c) THE GESTURE PATH CAN REPAIR AN INTERRUPTED CONTEXT
//
// The deeper half of thread 3973882771: `_resumeContext()` is what a play()
// reaches, and it called `resume()` on a `suspended` context ONLY. Every other
// non-running state fell into the throw, so `interrupted` -- the state WebKit
// uses when the output device is taken away, and the exact state this whole
// module exists for -- was the one the operator could not repair by hand.
//
// A source-text guard, because `audio-engine.svelte.ts` does not load under
// node:test (runes, stores, worklets); this is the same instrument every other
// engine drift guard in `tests/unit` uses, and `engineBlockAfter` refuses to
// return a body it did not positively locate.
//
// Regression lines:
// - if the gesture path stops attempting resume() on an interrupted context
//   then broken - pressing play cannot repair the state the device loss
//   actually produces
// - if it starts attempting resume() on a CLOSED context then broken - that
//   throws InvalidStateError and replaces the explicit message with a
//   DOMException
//-----------------------------------------------------------------------------

test('a hanging resume through resumeAudioContextOrReportDead records dead, toasts, and rethrows', async () => {
	const resumeHarness = await loadTypeScriptModule(
		'tests/unit/fixtures/resume-or-report-dead-entry.ts'
	);
	resumeHarness.resetPerfEventLog();
	const toastBefore = resumeHarness.toasts.length;
	const ctx = { resume: () => new Promise(() => {}) };
	await assert.rejects(
		resumeHarness.resumeAudioContextOrReportDead(ctx),
		(err) => {
			assert.equal(err?.name, 'AudioContextIoTimeoutError');
			return true;
		}
	);
	const dead = resumeHarness.readPerfEvents().find((row) => row.kind === 'audio-output-dead');
	assert.ok(dead, 'if resume times out then audio-output-dead must be recorded - broken');
	assert.equal(dead.severity, 'error');
	const toasts = resumeHarness.toasts.slice(toastBefore);
	assert.ok(
		toasts.some((toast) => toast.kind === 'error' && toast.message.includes('watchdog window')),
		'if resume times out then the watchdog-window error toast must fire - broken'
	);
});

test('the gesture resume path uses resumeAudioContextOrReportDead, not a bare ctx.resume()', () => {
	const body = engineBlockAfter('async function _resumeContext(): Promise<AudioContext> {');
	const gate = body
		.split('\n')
		.filter((line) => !line.trim().startsWith('//'))
		.join('\n');
	assert.ok(
		gate.includes('resumeAudioContextOrReportDead'),
		'if _resumeContext does not call resumeAudioContextOrReportDead then a clockless output can hang play forever - broken'
	);
	assert.ok(
		!/await\s+ctx\.resume\(\)/.test(gate),
		'if _resumeContext still awaits ctx.resume() directly then the IO timeout is bypassed - broken'
	);
});

test('the gesture resume path attempts an INTERRUPTED context, not only a suspended one', () => {
	const body = engineBlockAfter('async function _resumeContext(): Promise<AudioContext> {');
	const gate = body
		.split('\n')
		.filter((line) => !line.trim().startsWith('//'))
		.join('\n');
	assert.ok(
		gate.includes("=== 'interrupted'"),
		'if the gesture resume path never names interrupted then broken - it is the state ' +
			'WebKit uses when the device is taken away, so the one non-running state this ' +
			'module exists for is the one a play() cannot repair, and an operator whose ' +
			'watchdog schedule ran out has nothing left to press'
	);
	assert.ok(
		gate.includes("=== 'suspended'"),
		'if the gesture resume path stops resuming a SUSPENDED context then broken - that ' +
			'is the normal autoplay-policy state every context starts in, and nothing would ' +
			'ever start'
	);
	assert.ok(
		!gate.includes("=== 'closed'"),
		'if the gesture resume path starts resuming a CLOSED context then broken - resume() ' +
			'on a closed context throws InvalidStateError, which replaces the explicit ' +
			'"did not enter running state" message with a DOMException'
	);
	assert.ok(
		gate.includes('did not enter running state'),
		'if the explicit failure disappears then broken - a resume that is attempted and ' +
			'still fails must fail LOUDLY rather than returning a context that makes no sound'
	);
});

//-----------------------------------------------------------------------------
// (vii) the DOM edges are actually WIRED, not merely written
//
// Codex thread 3973514574 (P2 NON-BLOCKING) on PR #1644, and a real hole in
// this PR's own coverage. Every behavioural test above reaches the watchdog by
// calling `noteRecoveryOpportunity()` directly, and the soak fires its own fake
// `mediaDevices`. So deleting BOTH shipped listeners in
// `audio-context-instrumentation.ts` left the unit suite green, the soak green,
// and a context that exhausts its retries permanently silent again -- which is
// the entire failure AUDIOLIVE-04 exists to prevent.
//
// A source guard rather than a behavioural test, for the same reason `the graph
// arms the watchdog` above is one: the wiring lives in a module whose imports
// (`$lib/stores.svelte`, the perf ring, the meter worklet) do not load under
// node:test. What this pins is the CONNECTION, which is the part that was
// unpinned; the decision either side of it is driven for real above.
//-----------------------------------------------------------------------------

const INSTRUMENTATION_MODULE = 'src/lib/rb/audio-context-instrumentation.ts';

test('a hanging resume during watchdog recover continues the backoff and still give-up-toasts', async () => {
	const mod = _watchdog();
	const harness = fakeEffects();
	const perf = [];
	harness.effects.recordPerfEvent = (kind, message, severity) => {
		perf.push({ kind, message, severity });
	};
	const listeners = [];
	const ctx = fakeAudioContext();
	ctx.__nowMs = harness.nowMs;
	ctx.addEventListener = (type, handler) => {
		if (type === 'statechange') listeners.push(handler);
	};
	ctx.removeEventListener = () => {};
	ctx.resume = () => {
		ctx.resumeAtMs.push(ctx.__nowMs());
		return new Promise(() => {});
	};
	const detach = mod.installAudioContextWatchdog(ctx, harness.effects, () => true, 30);
	ctx.state = 'interrupted';
	for (const handler of listeners) handler();
	await settle();
	await _waitForWatchdogGiveUp({ harness, perf, ctx });
	assert.ok(
		ctx.resumeAtMs.length >= 2,
		'if a hanging resume blocks the recover loop then broken - the backoff must continue'
	);
	const errors = harness.toasts.filter((toast) => toast.kind === 'error');
	assert.ok(
		errors.some((toast) => toast.message.includes('did not come back')),
		'if every attempt times out then the existing give-up toast must still fire - broken'
	);
	assert.ok(
		perf.some((row) => row.kind === 'audio-output-dead' && row.severity === 'error'),
		'if the schedule exhausts on hung resumes then audio-output-dead must be recorded - broken'
	);
	detach();
});

test('both recovery edges are wired to noteRecoveryOpportunity, and armed', () => {
	const source = readFrontendSource(INSTRUMENTATION_MODULE);
	for (const [edge, why] of [
		[
			'visibilitychange',
			'a backgrounded WKWebView is the case that outlasts the 9,050ms schedule most often'
		],
		['devicechange', 'the device list moving is the only news that a device came back']
	]) {
		// BOTH halves, per edge. Asserting the bare string `'visibilitychange'`
		// was not enough: the name survives in the detach's removeEventListener,
		// so deleting the ADD left this green. Measured, not reasoned about --
		// the mutation was run and did not bite.
		assert.ok(
			source.includes(`addEventListener('${edge}'`),
			`if the instrumentation stops ADDING a '${edge}' listener then broken - ${why}, and ` +
				'without the edge the bounded schedule runs out and nothing ever asks again'
		);
		assert.ok(
			source.includes(`removeEventListener('${edge}'`),
			`if the '${edge}' listener is added but never REMOVED then broken - every route ` +
				'visit leaks another listener holding another closed context'
		);
	}
	assert.ok(
		source.includes('noteRecoveryOpportunity('),
		'if nothing calls noteRecoveryOpportunity then broken - the watchdog grows a re-arm ' +
			'path that no shipped code can reach, which catches exactly as many incidents as ' +
			'not having written it'
	);
	// The CALL, not the definition. `source.includes('installRecoveryOpportunities()')`
	// matches `function installRecoveryOpportunities(): void {` too, so the first
	// version of this assertion stayed GREEN with the call deleted -- a positive
	// answering an easier question than the one asked (verification.md). Measured,
	// not reasoned about: the mutation was run and did not bite.
	const calls = source
		.split('\n')
		.filter((line) => /^\s*(void\s+)?installRecoveryOpportunities\(\);/.test(line));
	assert.equal(
		calls.length,
		1,
		'if installRecoveryOpportunities is defined but never CALLED then broken - the ' +
			`listeners exist in the file and are attached to nothing. Found ${calls.length} call site(s)`
	);
	assert.ok(
		source.includes('_recoveryEdgesDetach'),
		'if the edges are attached with no detach held then broken - every route visit leaks ' +
			'another pair of listeners onto a context that has been closed'
	);
});
