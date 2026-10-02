/**
 * Continuous Beat Sync phase lock (NAE-19): the pure trim decision in
 * `rb/phase-lock.ts`, and a closed-loop simulation of a follower whose real
 * playback rate is off from its sync tempo.
 *
 * Regression lines:
 * - if a follower in phase gets any tempo other than its base then broken
 * - if an error inside the deadband leaves a trim in place then broken: the
 *   trim would accumulate into a permanent tempo change
 * - if a follower AHEAD is not slowed (or one behind not sped up) then broken
 * - if a trim exceeds the cap, or the deck's pitch range, then broken
 * - if a lost lock (a quarter beat, or PHASE_LOCK_RESEEK_MS, off - whichever
 *   is smaller) is trimmed instead of re-seeked then broken
 * - if a 0.05% rate error is not held within 5 ms over five minutes then
 *   broken; the control without the loop must drift past 100 ms, or the
 *   simulation is not testing anything
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let pl;

before(async () => {
	pl = await loadTypeScriptModule('src/lib/rb/phase-lock.ts');
});

/** A constant grid: `count` beats at `bpm` from `startSec`, bars of four. */
function grid(bpm, startSec, count) {
	const spb = 60 / bpm;
	return Array.from({ length: count }, (_, i) => ({ n: (i % 4) + 1, bpm, t: startSec + i * spb }));
}

const MASTER = grid(128, 0.1, 2000);
const FOLLOWER = grid(126, 0.05, 2000);
const BASE = 128 / 126;
const MASTER_BEAT_MS = (60 / 128) * 1000;

/** Follower position in phase with master position `mSec` (normalization 1). */
function inPhase(mSec) {
	const m = pl.gridBeatPosition(MASTER, mSec);
	const i = Math.floor(m);
	return FOLLOWER[i].t + (m - i) * (FOLLOWER[i + 1].t - FOLLOWER[i].t);
}

function input(mSec, fSec, extra = {}) {
	return {
		masterBeats: MASTER,
		masterPositionSec: mSec,
		masterTempo: 1,
		followerBeats: FOLLOWER,
		followerPositionSec: fSec,
		followerBaseTempo: BASE,
		pitchRangePct: 8,
		...extra
	};
}

test('zero error: the base tempo, exactly', () => {
	const d = pl.phaseLockDecision(input(60, inPhase(60)));
	assert.equal(d.action, 'base');
	assert.equal(d.tempo, BASE);
	assert.ok(Math.abs(d.errorMs) < 1e-6, `error ${d.errorMs}`);
});

test('an error inside the deadband plays the base tempo, never a leftover trim', () => {
	// 1.5 ms of wall clock ahead = 1.5 ms * base of follower track time.
	const d = pl.phaseLockDecision(input(60, inPhase(60) + 0.0015 * BASE));
	assert.equal(d.action, 'base');
	assert.equal(d.tempo, BASE);
	assert.ok(Math.abs(d.errorMs - 1.5) < 1e-6, `error ${d.errorMs}`);
	assert.ok(pl.phaseLockShouldSend(BASE * 1.002, d.tempo, BASE), 'a return to base is always sent');
	// Hysteresis: a trim in force holds down to the release threshold.
	const held = pl.phaseLockDecision(input(60, inPhase(60) + 0.0015 * BASE, { trimming: true }));
	assert.equal(held.action, 'trim');
	assert.ok(held.tempo < BASE);
	const released = pl.phaseLockDecision(input(60, inPhase(60) + 0.0005 * BASE, { trimming: true }));
	assert.equal(released.action, 'base');
	assert.equal(released.tempo, BASE);
});

test('ahead slows the follower, behind speeds it up, proportionally then capped', () => {
	const ahead = pl.phaseLockDecision(input(60, inPhase(60) + 0.0025 * BASE));
	assert.equal(ahead.action, 'trim');
	assert.ok(Math.abs(ahead.errorMs - 2.5) < 1e-6);
	const expected = BASE * (1 - 2.5 / (pl.PHASE_LOCK_CORRECTION_BEATS * MASTER_BEAT_MS));
	assert.ok(Math.abs(ahead.tempo - expected) < 1e-12, `${ahead.tempo} vs ${expected}`);
	assert.ok(ahead.tempo < BASE);

	const behind = pl.phaseLockDecision(input(60, inPhase(60) - 0.0025 * BASE));
	assert.equal(behind.action, 'trim');
	assert.ok(behind.errorMs < 0);
	assert.ok(behind.tempo > BASE);

	for (const sign of [1, -1]) {
		// 12 ms: past the 0.3% cap (which saturates near 5.6 ms) and still under
		// PHASE_LOCK_RESEEK_MS. It was 80 ms before the re-seek line moved from a
		// quarter beat alone to min(quarter beat, 15 ms): an 80 ms error is now a
		// re-join, not a 27 s trim (beat-sync-adversarial-phase-lock-recovery).
		const far = pl.phaseLockDecision(input(60, inPhase(60) + sign * 0.012 * BASE));
		assert.equal(far.action, 'trim', '12 ms is still under the re-seek line');
		const cap = BASE * (1 - sign * pl.PHASE_LOCK_MAX_TRIM);
		assert.ok(Math.abs(far.tempo - cap) < 1e-12, `capped at ${cap}, got ${far.tempo}`);
	}
});

test('a trim never leaves the pitch range', () => {
	// A base already at the top of a +-6% range: speeding up is clamped there.
	const d = pl.phaseLockDecision(
		input(60, inPhase(60) - 0.012 * BASE, { followerBaseTempo: 1.06, pitchRangePct: 6 })
	);
	assert.equal(d.action, 'trim');
	assert.equal(d.tempo, 1.06);
	const lo = pl.phaseLockDecision(
		input(60, inPhase(60) + 0.012 * BASE, { followerBaseTempo: 0.94, pitchRangePct: 6 })
	);
	assert.ok(Math.abs(lo.tempo - 0.94) < 1e-12, `clamped at the bottom, got ${lo.tempo}`);
});

test('a trim never goes below the 0.01 floor, even with a pitch range of 100% or more', () => {
	const d = pl.phaseLockDecision(
		input(60, inPhase(60) + 0.012 * BASE, { followerBaseTempo: 0.005, pitchRangePct: 100 })
	);
	assert.equal(d.action, 'trim');
	assert.equal(d.tempo, 0.01);
});

test('the feed-forward base ignores a non-finite or non-positive ask and keeps the base', () => {
	assert.equal(pl.phaseLockFeedForwardBase(input(60, inPhase(60), { masterTempo: Infinity })), BASE);
	assert.equal(pl.phaseLockFeedForwardBase(input(60, inPhase(60), { masterTempo: Number.NaN })), BASE);
	assert.equal(pl.phaseLockFeedForwardBase(input(60, inPhase(60), { masterTempo: 0 })), BASE);
	// Control: a real ask past the hysteresis does move it.
	assert.notEqual(pl.phaseLockFeedForwardBase(input(60, inPhase(60), { masterTempo: 1.02 })), BASE);
});

test('a lost lock asks for a re-seek, and the base meanwhile', () => {
	const d = pl.phaseLockDecision(input(60, inPhase(60) + 0.15 * BASE));
	assert.equal(d.action, 'reseek');
	assert.equal(d.tempo, BASE);
	// The ms line: 16 ms re-joins, 14 ms is still trimmed (both directions).
	assert.equal(pl.phaseLockDecision(input(60, inPhase(60) - 0.016 * BASE)).action, 'reseek');
	assert.equal(pl.phaseLockDecision(input(60, inPhase(60) + 0.014 * BASE)).action, 'trim');
	// Wrapped to the NEAREST master beat, never most of a beat of error.
	const wrap = pl.phaseErrorMs(input(60, inPhase(60) + 0.9 * MASTER_BEAT_MS * BASE / 1000));
	assert.ok(Math.abs(wrap + 0.1 * MASTER_BEAT_MS) < 0.01, `0.9 beat ahead is 0.1 behind, got ${wrap}`);
});

test('off the grid there is nothing to measure, and the base plays', () => {
	const d = pl.phaseLockDecision(input(0.05, inPhase(60)));
	assert.equal(d.action, 'unmeasured');
	assert.equal(d.tempo, BASE);
	assert.throws(() => pl.phaseLockDecision(input(60, 30, { followerBaseTempo: 0 })), /base tempo/);
});

test('phase is read through each deck own grid, so a variable grid measures true', () => {
	// The follower speeds up by 0.5% per beat; the same fraction of the same
	// beat must read as zero error even though no single BPM describes it.
	const variable = [];
	let t = 0.2;
	for (let i = 0; i < 400; i++) {
		variable.push({ n: (i % 4) + 1, bpm: 0, t });
		t += 0.5 * Math.pow(0.995, i % 50);
	}
	for (const mSec of [20.3, 41.77, 90.01]) {
		const m = pl.gridBeatPosition(MASTER, mSec);
		const i = Math.floor(m);
		const fSec = variable[i].t + (m - i) * (variable[i + 1].t - variable[i].t);
		const err = pl.phaseErrorMs(input(mSec, fSec, { followerBeats: variable }));
		assert.ok(Math.abs(err) < 1e-6, `variable grid error at ${mSec}: ${err}`);
	}
	// Half-tempo lock: two follower intervals per master beat.
	const double = grid(256, 0.05, 4000);
	const m = pl.gridBeatPosition(MASTER, 50);
	const f = 2 * m;
	const fi = Math.floor(f);
	const fSec = double[fi].t + (f - fi) * (double[fi + 1].t - double[fi].t);
	const err = pl.phaseErrorMs(input(50, fSec, { followerBeats: double, normalization: 2 }));
	assert.ok(Math.abs(err) < 1e-6, `normalization 2 error ${err}`);
});

test('phaseLockShouldSend: small moves are held, real ones and returns to base are sent', () => {
	assert.equal(pl.phaseLockShouldSend(BASE, BASE, BASE), false);
	assert.equal(pl.phaseLockShouldSend(BASE, BASE * 1.0001, BASE), false);
	assert.equal(pl.phaseLockShouldSend(BASE, BASE * 1.001, BASE), true);
	assert.equal(pl.phaseLockShouldSend(BASE * 1.0001, BASE, BASE), true);
});

/**
 * Five minutes at a 30 Hz tick. The follower's REAL rate is its commanded
 * tempo times (1 + rateError); a command lands one tick after it is decided
 * (the socket trip), and every reading carries up to +-0.5 ms of feed jitter.
 */
function simulate({ loop, rateError = 0.0005, seconds = 300 }) {
	const dt = 1 / 30;
	let mSec = 20;
	let fSec = inPhase(mSec);
	let commanded = BASE;
	let pending = null;
	let sends = 0;
	let reseeks = 0;
	let maxAbs = 0;
	let finalErr = 0;
	let seed = 12345;
	const jitterMs = () => {
		seed = (seed * 1103515245 + 12345) % 2147483648;
		return (seed / 2147483648 - 0.5) * 1.0;
	};
	for (let step = 0; step < seconds * 30; step++) {
		if (pending !== null) {
			commanded = pending;
			pending = null;
		}
		mSec += dt;
		fSec += dt * commanded * (1 + rateError);
		const trueErr = pl.phaseErrorMs(input(mSec, fSec));
		maxAbs = Math.max(maxAbs, Math.abs(trueErr));
		finalErr = trueErr;
		if (!loop) continue;
		const d = pl.phaseLockDecision(
			input(mSec, fSec + (jitterMs() / 1000) * BASE, { trimming: commanded !== BASE })
		);
		if (d.action === 'reseek') reseeks += 1;
		else if (pl.phaseLockShouldSend(commanded, d.tempo, BASE)) {
			pending = d.tempo;
			sends += 1;
		}
	}
	return { maxAbs, finalErr, sends, reseeks };
}

test('simulation: a 0.05% rate error is held under 5 ms for five minutes', () => {
	const r = simulate({ loop: true });
	assert.equal(r.reseeks, 0, 'the lock never had to re-seek');
	assert.ok(r.maxAbs < 5, `max phase error ${r.maxAbs.toFixed(2)} ms`);
	// About 9000 ticks: the resend threshold keeps commands well under one per tick.
	assert.ok(r.sends < 900, `${r.sends} tempo commands in five minutes`);
	assert.ok(r.sends > 0, 'the loop did act');
	console.log(
		`# phase lock on: max |error| ${r.maxAbs.toFixed(2)} ms, ${r.sends} tempo commands in 300 s`
	);
	const behind = simulate({ loop: true, rateError: -0.0005 });
	assert.ok(behind.maxAbs < 5, `slow follower max error ${behind.maxAbs.toFixed(2)} ms`);
	// The worst grid-rounding tempo error seen on real PQTZ, 0.15%.
	const worst = simulate({ loop: true, rateError: 0.0015 });
	assert.equal(worst.reseeks, 0);
	assert.ok(worst.maxAbs < 5, `0.15% max error ${worst.maxAbs.toFixed(2)} ms`);
	console.log(
		`# 0.15%: max |error| ${worst.maxAbs.toFixed(2)} ms, ${worst.sends} commands; ` +
			`-0.05%: ${behind.maxAbs.toFixed(2)} ms`
	);
});

test('control: without the loop the same 0.05% drifts past 100 ms', () => {
	const r = simulate({ loop: false });
	assert.ok(Math.abs(r.finalErr) > 100, `open-loop error after 300 s ${r.finalErr.toFixed(1)} ms`);
	console.log(`# phase lock off: error after 300 s ${r.finalErr.toFixed(1)} ms`);
});
