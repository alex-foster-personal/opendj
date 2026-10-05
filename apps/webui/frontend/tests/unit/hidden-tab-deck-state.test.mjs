// requirement: AUDIOLIVE-11
//
// Deck state must not depend on requestAnimationFrame. A browser stops firing
// rAF for a hidden, minimized or occluded tab while the AudioContext clock,
// timers and audio events keep running, and a DJ app spends much of a set in
// that state. This drives the REAL engine (real transport scheduling, real
// presentation timeline) over a Web Audio double whose clock runs in real time,
// with an animation clock the test controls.
//
// [if] the tab is hidden (no frame ever fires) and a playing deck is paused
//   [then] `audible` and `anyDeckPlaying()` go false once the stop reaches the
//   output [⛔️ if they stay true for ever]
// [if] the tab is hidden and a deck that has played is unloaded [then]
//   unload() resolves and the deck is empty [⛔️ if it never resolves]
// [if] the stop has been scheduled but has not reached the output yet [then]
//   `audible` is still true, hidden or visible [⛔️ if it is cleared early]
// [if] the tab is visible [then] all of the above hold on frames alone
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { after, afterEach, before, beforeEach, test } from 'node:test';

import { FakeAudioContext, installWindow } from './fixtures/fake-web-audio.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const API_BASE = 'https://hidden-tab-deck-state.example.test';
const SID = 'e'.repeat(40);
const STRETCH_STUB = fileURLToPath(new URL('./fixtures/stretch-deck-processor-load-stub.ts', import.meta.url));
/** How far the output lags the render clock. Wide on purpose: it is the window
 * in which a stop is scheduled but not yet heard, which the early-clear control
 * needs to be able to observe. */
const OUTPUT_LAG_SEC = 0.25;
/** Generous bound for "the stop has reached the output": schedule lead plus the
 * lag above plus timer slack on a loaded machine. */
const SETTLE_MS = 3000;

/** The harness keeps the real timers: the throttling case below takes the
 * page's away, and the test's own waits must not go with them. */
const realSetTimeout = globalThis.setTimeout;
const realSetInterval = globalThis.setInterval;

let audio;
let gate;
let stores;
let registry;
let perf;
let health;
const realFetch = globalThis.fetch;

//-----------------------------------------------------------------------------
// Web Audio double with a clock that runs, as a real output device's does
//-----------------------------------------------------------------------------

class LiveClockAudioContext extends FakeAudioContext {
	#startedAtMs = performance.now();
	get currentTime() { return (performance.now() - this.#startedAtMs) / 1000; }
	set currentTime(_value) { /* the base constructor assigns 0; the clock is derived */ }
	getOutputTimestamp() {
		return { contextTime: Math.max(0, this.currentTime - OUTPUT_LAG_SEC), performanceTime: performance.now() };
	}
	/** `ended` is an audio-thread event: it fires at the stop time whatever the
	 * animation clock is doing. */
	createConstantSource() {
		const node = super.createConstantSource();
		node.stop = (when = 0) => {
			realSetTimeout(() => node.onended?.(), Math.max(0, (when - this.currentTime) * 1000));
		};
		return node;
	}
}

//-----------------------------------------------------------------------------
// the animation clock, under the test's control
//-----------------------------------------------------------------------------

const frames = new Map();
let nextFrame = 0;
let visibleTimer = null;

function installAnimationClock() {
	globalThis.requestAnimationFrame = (callback) => {
		nextFrame += 1;
		frames.set(nextFrame, callback);
		return nextFrame;
	};
	globalThis.cancelAnimationFrame = (handle) => frames.delete(handle);
}

/** A visible tab: every pending frame fires at ~60 Hz. */
function showTab() {
	visibleTimer = realSetInterval(() => {
		const due = [...frames.values()];
		frames.clear();
		for (const callback of due) callback(performance.now());
	}, 16);
}

/** A hidden tab: frames are accepted and never fire. */
function hideTab() {
	if (visibleTimer !== null) clearInterval(visibleTimer);
	visibleTimer = null;
}

/**
 * A hidden tab under intensive timer throttling: the page reports hidden and
 * its timers do not fire inside the test's horizon (a real browser delays them
 * by up to a minute). Audio events are not timers and still arrive.
 */
function hideTabAndThrottleTimers() {
	hideTab();
	globalThis.document = { hidden: true, visibilityState: 'hidden', addEventListener() {}, removeEventListener() {} };
	globalThis.setTimeout = () => 0;
}

/**
 * Frames and timers both stop on a page that does NOT report hidden (an
 * occluded window is the shape). Nothing schedules an audio-clock wake here,
 * so a wait has only itself to rely on.
 */
function stallFramesAndTimersWithoutHiding() {
	hideTab();
	globalThis.setTimeout = () => 0;
}

function restorePage() {
	globalThis.setTimeout = realSetTimeout;
	delete globalThis.document;
}

const sleep = (ms) => new Promise((resolve) => realSetTimeout(resolve, ms));

async function until(predicate, what, timeoutMs = SETTLE_MS) {
	const deadline = performance.now() + timeoutMs;
	while (performance.now() < deadline) {
		if (predicate()) return;
		await sleep(10);
	}
	assert.fail(`${what} within ${timeoutMs} ms`);
}

/** Below unload()'s own 2000 ms presented-stop deadline ON PURPOSE: a wait that
 * only ends by timing out and force-unloading must fail here, not pass late. */
const UNLOAD_MS = 1500;

/** Reject rather than hang the whole file when a promise never settles. */
function bounded(promise, what, timeoutMs = UNLOAD_MS) {
	let timer;
	const timeout = new Promise((_, reject) => {
		timer = realSetTimeout(() => reject(new Error(`${what} did not settle within ${timeoutMs} ms`)), timeoutMs);
	});
	return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

function json(body, status = 200) {
	return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function installDaemon() {
	const bands = { length: 0, low: [], mid: [], high: [] };
	globalThis.fetch = async (input) => {
		const url = input instanceof Request ? input.url : String(input);
		if (url.includes('/anlz?')) {
			return json({
				stable_id: url.match(/[0-9a-f]{40}/)[0],
				points: 38_400,
				waveform: { kind: 'mono', preview: { ...bands }, detail: { ...bands } },
				beatgrid: { source: 'rekordbox', beat_count: 0, beats: [] },
				beatgrid_source: 'rekordbox',
				cues: [],
				phrases: [],
				vocals: { status: 'not_analyzed' },
				local_waveform: { status: 'decoded' }
			});
		}
		if (url.endsWith('/hot-cues')) {
			return json(['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'].map((slot) => ({ slot, cue: null, revision: `empty-${slot}` })));
		}
		// 60 s of audio at 48 kHz, so no case runs into the natural end.
		if (url.endsWith('/audio')) return new Response(new Uint8Array(4 * 48_000 * 60));
		const track = url.match(/\/tracks\/([0-9a-f]{40})(\/stems)?$/);
		if (track?.[2] !== undefined) return json({ detail: { code: 'STEMS_NOT_FOUND', message: 'no stems' } }, 404);
		if (track !== null) return json({ stable_id: track[1], title: 'Hidden Tab', artist: 'Fixture', bpm: null });
		throw new Error(`unexpected request ${url}`);
	};
}

before(async () => {
	installWindow('');
	globalThis.AudioContext = LiveClockAudioContext;
	installAnimationClock();
	({ audio, gate, stores, registry, perf, health } = await loadTypeScriptModule('tests/unit/fixtures/hidden-tab-entry.ts', {
		viteApiBase: API_BASE,
		alias: { '$lib/rb/stretch-adapter': STRETCH_STUB }
	}));
});

beforeEach(async () => {
	hideTab();
	frames.clear();
	await audio.engine.dispose();
	FakeAudioContext.instances = [];
	registry.resetAudioContextRegistryForTest();
	for (const toast of [...stores.toasts]) stores.dismissToast(toast.logId);
	installDaemon();
});

afterEach(() => {
	hideTab();
	restorePage();
	globalThis.fetch = realFetch;
});

after(async () => {
	await audio.engine.dispose();
	delete globalThis.AudioContext;
	delete globalThis.requestAnimationFrame;
	delete globalThis.cancelAnimationFrame;
	delete globalThis.window;
});

const deck = () => audio.getDeckState(1);

/** Load deck 1 and play it until the output is presenting it. */
async function playUntilAudible() {
	await audio.engine.load(1, SID);
	await audio.engine.play(1);
	assert.equal(deck().playing, true, 'precondition: play was scheduled');
	await until(() => deck().audible, 'if a playing deck never becomes audible then broken - the presented state did not advance');
	assert.equal(gate.anyDeckPlaying(), true, 'precondition: the playing gate sees the live deck');
}

/** The shared body: what must hold whether or not frames fire. */
async function pauseClearsAudibleOnlyOncePresented(onceAudible = () => {}) {
	await playUntilAudible();
	onceAudible();
	await audio.engine.pause(1);
	assert.equal(deck().playing, false, 'the pause was scheduled');
	assert.equal(
		deck().audible,
		true,
		'if audible is already false the moment pause() returns then broken - the stop is scheduled ahead and ' +
			`the output is still ${OUTPUT_LAG_SEC * 1000} ms behind, so the deck is still being heard`
	);
	assert.equal(gate.anyDeckPlaying(), true, 'the gate holds while the stop is still ringing out');
	await until(
		() => !deck().audible,
		'if audible stays true after the stop reached the output then broken - everything gated on "no deck audible" never runs'
	);
	assert.equal(deck().transport_pending, false, 'the stop is fully presented');
	assert.equal(
		gate.anyDeckPlaying(),
		false,
		'if anyDeckPlaying() still reads true after a presented pause then broken - background work stays shed for ever'
	);
}

async function unloadAfterPlayResolves(onceAudible = () => {}) {
	await playUntilAudible();
	onceAudible();
	await audio.engine.pause(1);
	await bounded(
		audio.engine.unload(1),
		'if unload() of a deck that has played never resolves then broken - unload'
	);
	assert.equal(deck().stable_id, null, 'the deck is empty');
	assert.equal(deck().audible, false);
	assert.equal(gate.anyDeckPlaying(), false);
}

async function unloadWhilePlayingResolves(onceAudible = () => {}) {
	await playUntilAudible();
	onceAudible();
	await bounded(
		audio.engine.unload(1),
		'if unload() of a PLAYING deck never resolves then broken - unload'
	);
	assert.equal(deck().stable_id, null, 'the deck is empty');
	assert.equal(gate.anyDeckPlaying(), false);
}

//-----------------------------------------------------------------------------
// the reported shape: the deck starts while the tab is visible, then the tab is
// hidden and no frame fires again
//-----------------------------------------------------------------------------

test('AUDIOLIVE-11: tab hidden after play - pause clears audible and anyDeckPlaying() once the stop is presented, not before', async () => {
	showTab();
	await pauseClearsAudibleOnlyOncePresented(hideTab);
});

test('AUDIOLIVE-11: tab hidden after play - unload() of a deck that played and paused resolves', async () => {
	showTab();
	await unloadAfterPlayResolves(hideTab);
});

test('AUDIOLIVE-11: tab hidden after play - unload() of a playing deck resolves', async () => {
	showTab();
	await unloadWhilePlayingResolves(hideTab);
});

//-----------------------------------------------------------------------------
// hidden throughout: the whole transport runs with no frame at all
//-----------------------------------------------------------------------------

test('AUDIOLIVE-11: hidden throughout - play becomes audible, stays audible, the position mirror advances, pause clears it', async () => {
	hideTab();
	await playUntilAudible();
	await sleep(600);
	assert.equal(deck().audible, true, 'if a deck that is still playing reads inaudible then broken - the fix overshot');
	assert.equal(gate.anyDeckPlaying(), true);
	assert.notEqual(
		health.audioHealthLevel(),
		'crit',
		'if backstop runs are counted as painted frames then broken - the audio-Hz meter reads ~10 Hz and goes red on every hidden tab'
	);
	const before = deck().position_ms;
	await until(
		() => deck().position_ms > before,
		'if the published position never advances while hidden then broken - readers of the mirror stall'
	);
	await audio.engine.pause(1);
	assert.equal(deck().audible, true, 'if audible is cleared before the stop is presented then broken');
	await until(() => !deck().audible, 'if audible stays true after a hidden pause then broken');
	assert.equal(gate.anyDeckPlaying(), false);
});

//-----------------------------------------------------------------------------
// hidden AND timer-throttled: only the audio clock is left
//-----------------------------------------------------------------------------

test('AUDIOLIVE-11: hidden tab with page timers throttled away - pause still clears audible, on the audio clock', async () => {
	showTab();
	await pauseClearsAudibleOnlyOncePresented(hideTabAndThrottleTimers);
});

test('AUDIOLIVE-11: hidden tab with page timers throttled away - unload() still resolves, on the audio clock', async () => {
	showTab();
	await unloadAfterPlayResolves(hideTabAndThrottleTimers);
	restorePage();
	showTab();
	await unloadWhilePlayingResolves(hideTabAndThrottleTimers);
});

test('AUDIOLIVE-11: frames and timers stall on a page that does not report hidden - unload() resolves by publishing from its own wait', async () => {
	showTab();
	await unloadAfterPlayResolves(stallFramesAndTimersWithoutHiding);
});

//-----------------------------------------------------------------------------
// the bounded timeout: a stop that never reaches the output
//-----------------------------------------------------------------------------

test('AUDIOLIVE-11: a stop the output never presents ends unload() at its deadline, loudly, and the deck is still unloaded', async () => {
	showTab();
	await playUntilAudible();
	// The output device stops reporting a position at all: no presented time and
	// no readable clock to time a stall against, so nothing can present the stop.
	FakeAudioContext.instances.at(-1).getOutputTimestamp = () => ({ contextTime: 0, performanceTime: Number.NaN });
	const timeouts = () => perf.readPerfEvents().filter((event) => event.kind === 'deck-unload-stop-timeout');
	assert.equal(timeouts().length, 0, 'control: no timeout has been recorded before the unload');
	const startedAt = performance.now();
	await bounded(audio.engine.unload(1), 'if unload() outlives its own deadline then broken - unload', 4000);
	const tookMs = performance.now() - startedAt;
	assert.ok(tookMs >= 1900, `if unload() gave up after ${Math.round(tookMs)} ms then broken - it force-unloaded a deck that was still being heard before the 2000 ms deadline`);
	assert.equal(deck().stable_id, null, 'if the deck is still loaded after the deadline then broken - a deck that cannot be ejected');
	assert.equal(gate.anyDeckPlaying(), false);
	const [event] = timeouts();
	assert.ok(event !== undefined, 'if the deadline passes without a recorded event then broken - a silent forced unload');
	assert.equal(event.severity, 'error', 'if it is recorded below error severity then broken - it never leaves the browser');
	assert.match(event.message, /did not reach a presented stop within 2000 ms/);
});

//-----------------------------------------------------------------------------
// visible tab controls: frames alone must still carry the same behavior
//-----------------------------------------------------------------------------

test('AUDIOLIVE-11 control: visible tab - pause clears audible and anyDeckPlaying() once the stop is presented, not before', async () => {
	showTab();
	await pauseClearsAudibleOnlyOncePresented();
});

test('AUDIOLIVE-11 control: visible tab - unload() resolves for a paused and for a playing deck', async () => {
	showTab();
	await unloadAfterPlayResolves();
	await unloadWhilePlayingResolves();
});
