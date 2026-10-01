/**
 * Beat Sync hardening (adversarial round 1, GREEN): inputs and grids chosen
 * to break `computeFollowerSyncPlan` and the phase lock, where the code holds.
 * Companion red files: beat-sync-adversarial-*.test.mjs.
 *
 * Regression lines:
 * - if a NaN / Infinity / zero / negative tempo or position reaches a plan
 *   instead of a RangeError naming the field then broken
 * - if an empty, single-beat, negative-time, duplicate-time or 3/4 grid is
 *   accepted then broken (the plan must refuse, never guess a grid)
 * - if a multi-segment (const_regions) grid joins off phase at or across a
 *   segment boundary then broken
 * - if a pitch range edge is not inclusive, or a ratio past it is accepted,
 *   then broken
 * - if 4 decks joined to one master do not share the master's phase then broken
 * - if the phase lock does not hold ten minutes of a constant grid tempo
 *   error at the worst real PQTZ rounding (0.15%) then broken
 * - if +-1 ms of playhead jitter makes the lock re-seek or send more than a
 *   few commands per second then broken
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let bsm;
let pl;

before(async () => {
	bsm = await loadTypeScriptModule('src/lib/rb/beat-sync-math.ts');
	pl = await loadTypeScriptModule('src/lib/rb/phase-lock.ts');
});

function grid(bpm, startSec, count, firstN = 1) {
	const spb = 60 / bpm;
	return Array.from({ length: count }, (_, i) => ({
		n: ((i + firstN - 1) % 4) + 1,
		bpm,
		t: startSec + i * spb
	}));
}

/** Concatenated constant-tempo segments, n cadence continuous. */
function segmented(startSec, segments) {
	const beats = [];
	let t = startSec;
	for (const [bpm, count] of segments) {
		for (let i = 0; i < count; i++) {
			beats.push({ n: (beats.length % 4) + 1, bpm, t });
			t += 60 / bpm;
		}
	}
	return beats;
}

const M128 = grid(128, 0.1, 1200);
const F126 = grid(126, 0.05, 1200);

function request(extra = {}) {
	return {
		masterGrid: M128,
		followerGrid: F126,
		masterPositionAtSyncSec: 61.234,
		masterTempoRatio: 1,
		followerPositionSec: 30,
		currentContextTimeSec: 10,
		syncAtContextTimeSec: 10.1,
		minFollowerTempoRatio: 0.92,
		maxFollowerTempoRatio: 1.08,
		mode: 'bar',
		...extra
	};
}

function phaseOf(beats, sec) {
	const b = pl.gridBeatPosition(beats, sec);
	const i = Math.floor(b);
	return { n: beats[i].n, frac: b - i };
}

test('non-finite, zero and negative numbers are refused by name', () => {
	const cases = [
		['masterTempoRatio', 0],
		['masterTempoRatio', -1],
		['masterTempoRatio', Number.NaN],
		['masterTempoRatio', Number.POSITIVE_INFINITY],
		['masterPositionAtSyncSec', Number.NaN],
		['masterPositionAtSyncSec', -0.001],
		['followerPositionSec', Number.POSITIVE_INFINITY],
		['minFollowerTempoRatio', 0],
		['maxFollowerTempoRatio', Number.NaN]
	];
	for (const [field, value] of cases) {
		assert.throws(
			() => bsm.computeFollowerSyncPlan(request({ [field]: value })),
			(error) => error instanceof RangeError && error.message.includes(field),
			`${field}=${value}`
		);
	}
	assert.throws(
		() => bsm.computeFollowerSyncPlan(request({ syncAtContextTimeSec: 10 })),
		/syncAtContextTimeSec must be after currentContextTimeSec/
	);
	assert.throws(
		() => bsm.computeFollowerSyncPlan(request({ minFollowerTempoRatio: 1.1, maxFollowerTempoRatio: 0.9 })),
		/inverted/
	);
	assert.throws(() => bsm.computeFollowerSyncPlan(request({ mode: 'phase' })), TypeError);
});

test('malformed grids are refused, never guessed', () => {
	const bad = {
		empty: [],
		single: [{ n: 1, bpm: 128, t: 0.1 }],
		negativeFirst: [{ n: 1, bpm: 128, t: -0.2 }, ...grid(128, 0.27, 20, 2)],
		duplicateTime: [{ n: 1, bpm: 128, t: 0.1 }, { n: 2, bpm: 128, t: 0.1 }, ...grid(128, 0.6, 20, 3)],
		threeFour: Array.from({ length: 30 }, (_, i) => ({ n: (i % 3) + 1, bpm: 128, t: 0.1 + i * 0.46875 })),
		zeroBpmField: grid(128, 0.1, 20).map((b, i) => (i === 5 ? { ...b, bpm: 0 } : b))
	};
	for (const [name, beats] of Object.entries(bad)) {
		assert.throws(() => bsm.validateBeatGrid(beats), RangeError, name);
		assert.throws(() => bsm.computeFollowerSyncPlan(request({ followerGrid: beats, followerPositionSec: 1 })), RangeError, name);
		assert.throws(() => bsm.computeFollowerSyncPlan(request({ masterGrid: beats, masterPositionAtSyncSec: 1 })), RangeError, name);
	}
});

test('a grid that starts on beat 3 still bar-locks on the master beat number', () => {
	const follower = grid(126, 0.05, 1200, 3);
	const p = bsm.computeFollowerSyncPlan(request({ followerGrid: follower }));
	const m = phaseOf(M128, 61.234);
	const f = phaseOf(follower, p.followerPositionSec);
	assert.equal(f.n, m.n);
	assert.ok(Math.abs(f.frac - m.frac) < 1e-9);
});

test('const_regions: joins land phase-exact before, on and after a segment boundary', () => {
	// Master 128 for 200 beats, then 132 - a real multi-region rekordbox grid.
	const master = segmented(0.1, [[128, 200], [132, 600]]);
	const boundary = master[200].t;
	for (const at of [boundary - 0.3, boundary, boundary + 0.01, boundary + 20]) {
		const p = bsm.computeFollowerSyncPlan(request({ masterGrid: master, masterPositionAtSyncSec: at }));
		const m = phaseOf(master, at);
		const f = phaseOf(F126, p.followerPositionSec);
		assert.equal(f.n, m.n, `at ${at}`);
		assert.ok(Math.abs(f.frac - m.frac) < 1e-9, `at ${at}: ${f.frac} vs ${m.frac}`);
	}
	// 40 beats past the boundary the window holds only 132 BPM intervals.
	const deep = bsm.computeFollowerSyncPlan(request({ masterGrid: master, masterPositionAtSyncSec: master[240].t + 0.1 }));
	assert.ok(Math.abs(deep.followerTempoRatio - 132 / 126) < 1e-9, `${deep.followerTempoRatio}`);
});

test('pitch range edges are inclusive and the next step past them is refused', () => {
	// 126 -> 136.08 BPM is exactly +8%.
	const edge = bsm.computeFollowerSyncPlan(request({ masterTempoRatio: 136.08 / 128 }));
	assert.ok(Math.abs(edge.followerTempoRatio - 1.08) < 1e-9);
	assert.throws(
		() => bsm.computeFollowerSyncPlan(request({ masterTempoRatio: 136.2 / 128 })),
		/no phase-capable bar anchor with tempo ratio within \[0\.92, 1\.08\]/
	);
});

test('four decks joined to one master share its phase, pairwise', () => {
	const followers = [grid(126, 0.05, 1200), grid(124, 0.3, 1200, 2), grid(130, 0.11, 1200, 4)];
	const m = phaseOf(M128, 61.234);
	for (const [k, followerGrid] of followers.entries()) {
		const p = bsm.computeFollowerSyncPlan(request({ followerGrid, followerPositionSec: 20 + k * 7 }));
		const f = phaseOf(followerGrid, p.followerPositionSec);
		assert.equal(f.n, m.n, `follower ${k}`);
		assert.ok(Math.abs(f.frac - m.frac) < 1e-9, `follower ${k}`);
	}
});

/** Closed loop at 30 Hz; returns the worst |error| after the first 8 s. */
function simulate({ masterGrid, followerGrid, seconds, lock, jitterMs = 0, seed = 1, audioRateError = 0 }) {
	let rnd = seed;
	const noise = () => {
		rnd = (rnd * 16807) % 2147483647;
		return ((rnd / 2147483647) * 2 - 1) * (jitterMs / 1000);
	};
	const tick = 1 / 30;
	let m = 20;
	const join = bsm.computeFollowerSyncPlan({
		masterGrid,
		followerGrid,
		masterPositionAtSyncSec: m,
		masterTempoRatio: 1,
		followerPositionSec: 20,
		currentContextTimeSec: 0,
		syncAtContextTimeSec: 0.01,
		minFollowerTempoRatio: 0.92,
		maxFollowerTempoRatio: 1.08,
		mode: 'beat'
	});
	let f = join.followerPositionSec;
	const base = join.followerTempoRatio;
	let sent = base;
	let worst = 0;
	let reseeks = 0;
	let sends = 0;
	let overLineTicks = 0;
	for (let k = 0; k < seconds * 30; k++) {
		const input = {
			masterBeats: masterGrid,
			masterPositionSec: m + noise(),
			masterTempo: 1,
			followerBeats: followerGrid,
			followerPositionSec: f + noise(),
			followerBaseTempo: base,
			pitchRangePct: 8,
			trimming: sent !== base,
			sinceJoinSec: 600,
			overLineTicks
		};
		const truth = pl.phaseErrorMs({ ...input, masterPositionSec: m, followerPositionSec: f });
		if (truth === null) break;
		if (k * tick > 8) worst = Math.max(worst, Math.abs(truth));
		if (lock) {
			const d = pl.phaseLockDecision(input);
			overLineTicks = d.overLineTicks;
			if (d.action === 'reseek') reseeks += 1;
			else if (pl.phaseLockShouldSend(sent, d.tempo, base)) {
				sent = d.tempo;
				sends += 1;
			}
		}
		// Playback: the master plays its grid exactly; the follower's AUDIO runs
		// `audioRateError` off its grid (grid rounding, stretcher error).
		m += tick;
		f += tick * sent * (1 + audioRateError);
	}
	return { worst, reseeks, sendsPerSec: sends / seconds };
}

test('a constant 0.15% grid-vs-audio error (worst real PQTZ rounding) is held for ten minutes', () => {
	const locked = simulate({ masterGrid: M128, followerGrid: F126, seconds: 600, lock: true, audioRateError: 0.0015 });
	const free = simulate({ masterGrid: M128, followerGrid: F126, seconds: 600, lock: false, audioRateError: 0.0015 });
	assert.ok(free.worst > 100, `control must drift: ${free.worst} ms`);
	assert.equal(locked.reseeks, 0);
	assert.ok(locked.worst < 8, `locked worst ${locked.worst} ms`);
});

test('+-1 ms of playhead jitter: no re-seek, a handful of commands per second', () => {
	const r = simulate({ masterGrid: M128, followerGrid: F126, seconds: 120, lock: true, jitterMs: 1 });
	assert.equal(r.reseeks, 0);
	assert.ok(r.sendsPerSec < 3, `${r.sendsPerSec} commands/s`);
	assert.ok(r.worst < 4, `${r.worst} ms`);
});
