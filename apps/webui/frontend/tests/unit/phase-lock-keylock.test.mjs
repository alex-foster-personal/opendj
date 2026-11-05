/**
 * A KEY-LOCKED Beat Sync follower (Master Tempo on) corrects phase by tempo,
 * not by seeking (`rb/phase-lock.ts`, PHASE_LOCK_KEYLOCK_*). Its tempo does not
 * move its pitch, so the 0.3% varispeed cap has nothing to protect, and a seek
 * is an audible discontinuity a bend is not ((private path)).
 *
 * Regression lines:
 * - if a key-locked follower 15 ms to a quarter beat off is re-seeked instead
 *   of bent then broken; the control (key lock off) must still re-seek it
 * - if a key-locked follower past a quarter beat is bent instead of re-seeked
 *   then broken: that is the line where one seek beats seconds of flam
 * - if a key-locked trim exceeds PHASE_LOCK_KEYLOCK_MAX_TRIM or the pitch
 *   range, or a varispeed trim exceeds PHASE_LOCK_MAX_TRIM, then broken
 * - if a key-locked follower 60 ms off is not under 10 ms within 2 s, with no
 *   seek, then broken; the varispeed control must re-seek it
 * - if steady key-locked play under feed jitter sends more tempo commands than
 *   the varispeed lock's own budget, or loses the 5 ms hold, then broken
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let pl;

before(async () => {
	pl = await loadTypeScriptModule('src/lib/rb/phase-lock.ts');
});

function grid(bpm, startSec, count) {
	const spb = 60 / bpm;
	return Array.from({ length: count }, (_, i) => ({ n: (i % 4) + 1, bpm, t: startSec + i * spb }));
}

const MASTER = grid(128, 0.1, 2000);
const FOLLOWER = grid(126, 0.05, 2000);
const BASE = 128 / 126;
const MASTER_BEAT_MS = (60 / 128) * 1000;

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
		sinceJoinSec: 600,
		overLineTicks: 0,
		...extra
	};
}

/** Follower `ms` of wall clock ahead of the master's phase at 60 s. */
const ahead = (ms) => inPhase(60) + (ms / 1000) * BASE;
const confirming = { overLineTicks: 2 };

test('a key-locked follower between the ms line and a quarter beat is bent, not re-seeked', () => {
	for (const ms of [16, 30, 60, 100, -100]) {
		const locked = pl.phaseLockDecision(input(60, ahead(ms), { ...confirming, keyLocked: true }));
		assert.equal(locked.action, 'trim', `${ms} ms key-locked`);
		// Ahead slows, behind speeds up.
		assert.equal(Math.sign(locked.tempo - BASE), -Math.sign(ms), `${ms} ms direction`);
		// Control: the same error with key lock off is past the 15 ms line.
		const varispeed = pl.phaseLockDecision(input(60, ahead(ms), confirming));
		assert.equal(varispeed.action, 'reseek', `${ms} ms varispeed`);
	}
});

test('past a quarter beat even a key-locked follower re-seeks', () => {
	const ms = 0.26 * MASTER_BEAT_MS;
	assert.equal(
		pl.phaseLockDecision(input(60, ahead(ms), { ...confirming, keyLocked: true })).action,
		'reseek'
	);
	// Control: just inside the quarter beat it is bent.
	const inside = 0.24 * MASTER_BEAT_MS;
	assert.equal(
		pl.phaseLockDecision(input(60, ahead(inside), { ...confirming, keyLocked: true })).action,
		'trim'
	);
	// The re-join gates still hold: an unconfirmed tick, or one too soon after
	// the join, bends meanwhile.
	assert.equal(pl.phaseLockDecision(input(60, ahead(ms), { keyLocked: true })).action, 'trim');
	assert.equal(
		pl.phaseLockDecision(input(60, ahead(ms), { ...confirming, keyLocked: true, sinceJoinSec: 1 }))
			.action,
		'trim'
	);
});

test('the key-locked bend adds a two-beat gain past the near line, capped at 5%; varispeed keeps 0.3%', () => {
	// Inside the near line the gain is the varispeed lock's (four beats), so
	// steady play under jitter sends no more commands.
	const near = pl.phaseLockDecision(input(60, ahead(4), { keyLocked: true }));
	const wantNear = BASE * (1 - 4 / (pl.PHASE_LOCK_CORRECTION_BEATS * MASTER_BEAT_MS));
	assert.ok(Math.abs(near.tempo - wantNear) < 1e-9, `4 ms: ${near.tempo} want ${wantNear}`);
	// 40 ms: the near 5 over four beats plus the 35 beyond over two.
	const small = pl.phaseLockDecision(input(60, ahead(40), { keyLocked: true }));
	const nearMs = pl.PHASE_LOCK_KEYLOCK_NEAR_MS;
	const want =
		BASE *
		(1 -
			nearMs / (pl.PHASE_LOCK_CORRECTION_BEATS * MASTER_BEAT_MS) -
			(40 - nearMs) / (pl.PHASE_LOCK_KEYLOCK_CORRECTION_BEATS * MASTER_BEAT_MS));
	assert.ok(Math.abs(small.tempo - want) < 1e-9, `40 ms: ${small.tempo} want ${want}`);
	// 100 ms asks for more than 5%: capped.
	const big = pl.phaseLockDecision(input(60, ahead(100), { keyLocked: true }));
	assert.ok(
		Math.abs(big.tempo - BASE * (1 - pl.PHASE_LOCK_KEYLOCK_MAX_TRIM)) < 1e-9,
		`100 ms: ${big.tempo}`
	);
	// Control: the same 40 ms on a varispeed deck is the 0.3% cap's business.
	const vs = pl.phaseLockDecision(input(60, ahead(40)));
	assert.ok(
		Math.abs(vs.tempo / BASE - 1) <= pl.PHASE_LOCK_MAX_TRIM + 1e-12,
		`varispeed trim ${vs.tempo / BASE - 1}`
	);
	assert.ok(
		Math.abs(small.tempo / BASE - 1) > pl.PHASE_LOCK_MAX_TRIM,
		'the key-locked bend is the larger one'
	);
	// The pitch range still bounds it: a 2% fader keeps a 5% ask inside 2%.
	const narrow = pl.phaseLockDecision(
		input(60, ahead(-100), { keyLocked: true, pitchRangePct: 2 })
	);
	assert.ok(narrow.tempo <= 1.02 + 1e-12, `2% range: ${narrow.tempo}`);
});

/**
 * 30 Hz ticks. The follower starts `startMs` off; its real rate is the
 * commanded tempo times (1 + rateError); a command lands one tick after it is
 * decided; readings carry up to +-0.5 ms of jitter. A re-seek puts it back in
 * phase on the next tick, as the join does.
 */
function simulate({ keyLocked, startMs = 0, rateError = 0, seconds = 10, settleSec = 3 }) {
	const dt = 1 / 30;
	let mSec = 20;
	let fSec = inPhase(mSec) + (startMs / 1000) * BASE;
	let commanded = BASE;
	let pending = null;
	let reseekNext = false;
	let sends = 0;
	let reseeks = 0;
	let overLineTicks = 0;
	let under10At = null;
	let maxAfterSettle = 0;
	let seed = 12345;
	const jitterMs = () => {
		seed = (seed * 1103515245 + 12345) % 2147483648;
		return (seed / 2147483648 - 0.5) * 1.0;
	};
	for (let step = 0; step < seconds * 30; step++) {
		const t = step * dt;
		if (pending !== null) {
			commanded = pending;
			pending = null;
		}
		mSec += dt;
		fSec += dt * commanded * (1 + rateError);
		if (reseekNext) {
			fSec = inPhase(mSec);
			commanded = BASE;
			reseekNext = false;
		}
		const err = pl.phaseErrorMs(input(mSec, fSec));
		if (under10At === null && Math.abs(err) < 10) under10At = t;
		if (t > settleSec) maxAfterSettle = Math.max(maxAfterSettle, Math.abs(err));
		const d = pl.phaseLockDecision(
			input(mSec, fSec + (jitterMs() / 1000) * BASE, {
				trimming: commanded !== BASE,
				overLineTicks,
				keyLocked
			})
		);
		overLineTicks = d.overLineTicks;
		if (d.action === 'reseek') {
			reseeks += 1;
			reseekNext = true;
			overLineTicks = 0;
		} else if (pl.phaseLockShouldSend(commanded, d.tempo, BASE)) {
			pending = d.tempo;
			sends += 1;
		}
	}
	return { under10At, maxAfterSettle, sends, reseeks };
}

test('simulation: 60 ms off, key-locked, is bent under 10 ms within 2 s (100 ms within 3) and never seeks', () => {
	// 100 ms needs 2 s at the 5% cap alone (50 ms a second), so it gets 3.
	for (const [startMs, within] of [
		[60, 2],
		[-60, 2],
		[100, 3]
	]) {
		const r = simulate({ keyLocked: true, startMs, settleSec: within + 1 });
		assert.equal(r.reseeks, 0, `${startMs} ms: re-seeked`);
		assert.ok(
			r.under10At !== null && r.under10At < within,
			`${startMs} ms: under 10 ms at ${r.under10At} s`
		);
		assert.ok(
			r.maxAfterSettle < 5,
			`${startMs} ms: ${r.maxAfterSettle.toFixed(2)} ms after settling`
		);
		console.log(
			`# key-locked ${startMs} ms: under 10 ms at ${r.under10At.toFixed(2)} s, ${r.sends} commands`
		);
	}
	// Control: key lock off, the same 60 ms is past the 15 ms line and seeks.
	const varispeed = simulate({ keyLocked: false, startMs: 60 });
	assert.ok(varispeed.reseeks > 0, 'the varispeed lock should have re-seeked 60 ms');
});

test('simulation: steady key-locked play holds 5 ms under jitter within the varispeed command budget', () => {
	for (const rateError of [0.0005, -0.0005, 0.0015]) {
		const locked = simulate({ keyLocked: true, rateError, seconds: 300 });
		const varispeed = simulate({ keyLocked: false, rateError, seconds: 300 });
		assert.equal(locked.reseeks, 0, `${rateError}: re-seeked`);
		assert.ok(locked.maxAfterSettle < 5, `${rateError}: ${locked.maxAfterSettle.toFixed(2)} ms`);
		// The varispeed lock's own budget (phase-lock.test.mjs): under 900 in 300 s.
		assert.ok(locked.sends < 900, `${rateError}: ${locked.sends} commands in 300 s`);
		console.log(
			`# steady ${rateError}: key-locked max ${locked.maxAfterSettle.toFixed(2)} ms / ${locked.sends} commands; ` +
				`varispeed ${varispeed.maxAfterSettle.toFixed(2)} ms / ${varispeed.sends}`
		);
	}
});
