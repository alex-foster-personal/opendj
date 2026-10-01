/**
 * PERFMODE-18: the hold on a deck's own stem decode is graded by pressure,
 * and a finished cloud fetch is noticed within a second.
 *
 * Root cause measured Thu 1 Oct 2026 on the live preview host. The hold was
 * already skipped when the shed read no pressure, but that host reads
 * `churn_score` 5,820 to 12,933 against the 500 early-warning threshold while
 * the kernel reports memory pressure level 1 ("fine"). So the shed deferred
 * every decode started while a deck played, nothing released it, and each
 * one sat out the full flat 6,000 ms bound ("STEMS WAITING").
 *
 * Timing table this file asserts (hold before decode, playing deck):
 *
 *   signal while a deck plays            before     after
 *   no pressure                              0 ms       0 ms
 *   churn early warning only             6,000 ms     500 ms
 *   kernel memory pressure level >= 2    6,000 ms   2,000 ms
 *   an xrun in the current window        6,000 ms   2,000 ms
 *
 * The real createBackgroundDemandShed drives the real stem-decode-shed; the
 * only injected parts are the signals and the timer.
 *
 * Regression lines:
 *   - if a decode is held with no pressure signal then broken
 *   - if a churn-only early warning holds a decode longer than 500 ms then broken
 *   - if kernel pressure or an xrun holds a decode longer than 2 s then broken
 *   - if kernel pressure or an xrun gets the short early-warning hold then broken
 *   - if a held decode is dropped rather than started at the bound then broken
 *   - if a finished cloud fetch can go unnoticed for more than 1 s in its first minute then broken
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const BEFORE_FLAT_BOUND_MS = 6000;

let gateModule;
let shedModule;
let waitModule;

before(async () => {
	const options = { viteApiBase: 'https://stem-hold.example.test' };
	gateModule = await loadTypeScriptModule('src/lib/rb/playing-gate.ts', options);
	shedModule = await loadTypeScriptModule('src/lib/rb/stem-decode-shed.ts', options);
	waitModule = await loadTypeScriptModule('src/lib/rb/stem-hydrate-wait.ts', options);
});

/** Arms the real shed with the given signals and measures one decode's hold
 * on a virtual clock: the timer fires at once and reports what it was armed for. */
async function measureHold({ playing, churn, kernel, xrunsDuringWindow }) {
	const state = { xruns: 0 };
	const shed = gateModule.createBackgroundDemandShed({
		isPlaying: () => playing,
		pressureElevated: () => churn || kernel,
		readXruns: () => state.xruns,
		jobs: [{ id: 'eager-stem-decode', run: shedModule.resumeEagerStemDecodeOwedJob }],
		record: () => {}
	});
	shed.sync();
	state.xruns += xrunsDuringWindow;
	shedModule.setEagerStemDecodeShed(shed, () => kernel);
	let heldMs = 0;
	let deferred = 0;
	const start = await shedModule.awaitEagerStemDecodeSlot({
		onDeferred: () => (deferred += 1),
		setTimer: (run, ms) => {
			heldMs = ms;
			queueMicrotask(run);
			return null;
		},
		clearTimer: () => {}
	});
	shedModule.releaseEagerStemDecodeNow();
	shedModule.setEagerStemDecodeShed(null);
	return { start, heldMs, deferred };
}

test('no pressure: the decode starts at once, with no hold and no waiting state', async () => {
	const hold = await measureHold({ playing: true, churn: false, kernel: false, xrunsDuringWindow: 0 });
	assert.deepEqual(hold, { start: 'immediate', heldMs: 0, deferred: 0 });
});

test('churn early warning only: the hold is 500 ms, down from the flat 6,000 ms', async () => {
	const hold = await measureHold({ playing: true, churn: true, kernel: false, xrunsDuringWindow: 0 });
	assert.equal(hold.start, 'timed_out', 'the decode is started at the bound, never dropped');
	assert.equal(hold.deferred, 1);
	assert.equal(hold.heldMs, 500);
	assert.equal(hold.heldMs, shedModule.EAGER_STEM_DECODE_EARLY_WARNING_DEFER_MS);
	assert.ok(hold.heldMs < BEFORE_FLAT_BOUND_MS);
});

test('kernel memory pressure: the hold is 2,000 ms, down from the flat 6,000 ms', async () => {
	const hold = await measureHold({ playing: true, churn: true, kernel: true, xrunsDuringWindow: 0 });
	assert.equal(hold.start, 'timed_out');
	assert.equal(hold.heldMs, 2000);
	assert.equal(hold.heldMs, shedModule.EAGER_STEM_DECODE_MAX_DEFER_MS);
});

test('an xrun in the current window is real pressure too: 2,000 ms, with no kernel signal', async () => {
	const hold = await measureHold({ playing: true, churn: false, kernel: false, xrunsDuringWindow: 1 });
	assert.equal(hold.start, 'timed_out');
	assert.equal(hold.heldMs, 2000);
});

test('overshoot control: real pressure never gets the short early-warning hold', async () => {
	for (const signals of [
		{ churn: false, kernel: true, xrunsDuringWindow: 0 },
		{ churn: true, kernel: false, xrunsDuringWindow: 3 }
	]) {
		const hold = await measureHold({ playing: true, ...signals });
		assert.ok(hold.heldMs > shedModule.EAGER_STEM_DECODE_EARLY_WARNING_DEFER_MS, JSON.stringify(signals));
	}
});

test('control: with nothing playing even severe pressure holds nothing', async () => {
	const hold = await measureHold({ playing: false, churn: true, kernel: true, xrunsDuringWindow: 2 });
	assert.deepEqual(hold, { start: 'immediate', heldMs: 0, deferred: 0 });
});

// ---------------------------------------------------- cloud fetch poll cadence

/** Longest time a fetch that finishes `waitedMs` into the wait can go unnoticed. */
function worstLagMs(delays, waitedMs) {
	let at = 0;
	for (const delay of delays) {
		if (at + delay > waitedMs) return delay;
		at += delay;
	}
	throw new Error('schedule too short for the probe');
}

test('a finished cloud fetch is noticed within 1 s in its first minute (was up to 5 s)', () => {
	const after = [];
	let waited = 0;
	for (let attempt = 0; waited < 60_000; attempt += 1) {
		const delay = waitModule.stemHydratePollDelayMs(attempt, waited);
		after.push(delay);
		waited += delay;
	}
	const before = [1000, 2000, 3000, ...Array.from({ length: 20 }, () => 5000)];
	for (const finishedAt of [400, 3000, 9000, 17_000, 45_000]) {
		assert.ok(worstLagMs(after, finishedAt) <= 1000, `lag at ${finishedAt} ms`);
	}
	assert.equal(worstLagMs(before, 9000), 5000, 'control: the earlier schedule lagged up to 5 s');
	assert.equal(worstLagMs(after, 9000), 1000);
});

test('overshoot control: a download past its first minute backs off to one GET per 5 s', () => {
	assert.equal(waitModule.stemHydratePollDelayMs(70, 60_000), 5000);
	assert.equal(waitModule.stemHydratePollDelayMs(400, 9 * 60_000), 5000);
});
