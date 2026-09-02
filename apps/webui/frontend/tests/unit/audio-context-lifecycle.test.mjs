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
			}
		}
	};
}

/** Let the watchdog's async recovery loop run to completion. */
async function settle() {
	for (let i = 0; i < 200; i += 1) await Promise.resolve();
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
