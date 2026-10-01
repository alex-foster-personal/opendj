/**
 * The Beat Sync phase lock on a jittered grid (BEATSYNC-JITTER-01..05).
 *
 * Before/after on ONE simulation and ONE set of synthetic grids
 * (`phase-lock-workload.mjs`, the ADR deck-beatgrid-source Amendment 1 model):
 * "before" is the lock as it was (`legacyPhaseLockDecision`), "after" is the
 * shipped `phaseLockDecision`.
 *
 * Regression lines:
 * - if a steady grid stored in 5 or 10 ms steps is re-joined even once, or
 *   trimmed more than a tenth of the time, then broken (the lock is chasing
 *   storage dither again)
 * - if the same grids do NOT re-join under the legacy decision then the
 *   simulation is not testing anything
 * - if an even grid gets a different action, tempo or error than before then
 *   broken (the control: smoothing must not touch a grid that is not uneven)
 * - if a real tempo ramp or step is not followed within
 *   PHASE_LOCK_SMOOTH_HALF_WINDOW_BEATS + 1 beats, or is followed with a seek,
 *   then broken (smoothing made the lock deaf)
 * - if an offset the DJ dialed in is trimmed back or re-joined then broken; if
 *   the same offset NOT dialed in by the DJ is left standing then broken
 * - if a genuine large offset is not re-joined, or is re-joined more than
 *   once, then broken
 * - if a re-join is asked for on fewer than PHASE_LOCK_REJOIN_CONFIRM_TICKS
 *   ticks past the line, or sooner than PHASE_LOCK_REJOIN_MIN_INTERVAL_SEC
 *   after a join, then broken; if it is never asked for, then broken
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';
import {
	evenGrid,
	legacyPhaseLockDecision,
	lcg,
	lockWorkload,
	median,
	storedGrid,
	storedPhaseErrorMs,
	trueBeatTimes
} from './phase-lock-workload.mjs';

let pl;

before(async () => {
	pl = await loadTypeScriptModule('src/lib/rb/phase-lock.ts');
});

// ------------------------------------------------------------ fixtures

/** Steady music, stored the way a dynamic grid stores it:
 * `[bpm, stepMs, jitterMs, strayShare, strayMs]`. */
const JITTERED_SPECS = [
	[123.5, 10, 4, 0.05, 25],
	[126.03, 10, 6, 0.05, 30],
	[128.4, 10, 3, 0.03, 20],
	[91.7, 10, 8, 0.05, 40],
	[140.2, 5, 4, 0.05, 25],
	[104.6, 5, 8, 0.08, 30],
	[128.0, 10, 12, 0.05, 50]
];

function jittered([bpm, stepMs, jitterMs, strayShare, strayMs], seed) {
	const truth = trueBeatTimes({ beats: 520, bpmAt: () => bpm });
	return {
		truth: truth.map((t, i) => ({ n: (i % 4) + 1, bpm: 0, t })),
		stored: storedGrid(truth, { stepMs, jitterMs, strayShare, strayMs, seed })
	};
}

const JITTERED = JITTERED_SPECS.map((spec, index) => jittered(spec, 101 + index));

/** Even grids in millisecond storage, as rekordbox writes a fixed-tempo track. */
const EVEN = [120, 126.0, 128, 174].map((bpm) =>
	storedGrid(trueBeatTimes({ beats: 520, bpmAt: () => bpm }), { stepMs: 1 })
);

const MASTER_BPM = 120;
const RAMP_BEATS = { from: 200, to: 232 };
/** 120 to 123 BPM (2.5%) over 32 beats, then steady: a real tempo change. */
function rampBpm(i) {
	if (i < RAMP_BEATS.from) return 120;
	else if (i >= RAMP_BEATS.to) return 123;
	return 120 + (3 * (i - RAMP_BEATS.from)) / (RAMP_BEATS.to - RAMP_BEATS.from);
}
const STEP_BEAT = 250;
/** 120 to 122 BPM in one beat: the hardest real change for a smoothed measure. */
const stepBpm = (i) => (i < STEP_BEAT ? 120 : 122);

function variableTempo(bpmAt, seed) {
	const truth = trueBeatTimes({ beats: 520, bpmAt });
	return {
		truth: truth.map((t, i) => ({ n: (i % 4) + 1, bpm: 0, t })),
		stored: storedGrid(truth, { stepMs: 10, jitterMs: 4, seed }),
		master: evenGrid(60 / MASTER_BPM, 700)
	};
}

const after = (overrides) => lockWorkload({ decide: pl.phaseLockDecision, ...overrides });
const beforeChange = (overrides) => lockWorkload({ decide: legacyPhaseLockDecision, ...overrides });

function decisionInput(masterBeats, followerBeats, masterSec, followerSec, extra = {}) {
	return {
		masterBeats,
		masterPositionSec: masterSec,
		masterTempo: 1,
		followerBeats,
		followerPositionSec: followerSec,
		followerBaseTempo: 1,
		pitchRangePct: 8,
		sinceJoinSec: 600,
		overLineTicks: 0,
		userOffsetMs: 0,
		...extra
	};
}

// ------------------------------------------------- jittered, steady grids

test('a steady grid stored in 5 or 10 ms steps: no re-join, and trimming falls sharply', () => {
	const rows = JITTERED.map(({ stored, truth }) => ({
		before: beforeChange({ followerBeats: stored, truthBeats: truth }),
		after: after({ followerBeats: stored, truthBeats: truth })
	}));
	for (const [index, row] of rows.entries()) {
		const name = `grid ${JITTERED_SPECS[index].join('/')}`;
		assert.ok(row.before.reseeks > 0, `${name}: the legacy lock re-joins (else nothing is under test)`);
		assert.equal(row.after.reseeks, 0, `${name}: no re-join`);
		assert.ok(row.after.trimTimeFrac < 0.1, `${name}: trimming ${(row.after.trimTimeFrac * 100).toFixed(1)}% of the time`);
		assert.ok(
			row.after.trimTimeFrac < row.before.trimTimeFrac / 4,
			`${name}: trimming ${row.after.trimTimeFrac} against ${row.before.trimTimeFrac} before`
		);
		// The audio stays on the beat while it is left alone: inside the lock's own
		// deadband of where the chasing lock held it, and far inside a flam (10 ms).
		assert.ok(
			row.after.medianTruthErrMs <= row.before.medianTruthErrMs + pl.PHASE_LOCK_DEADBAND_MS,
			`${name}: audio off by ${row.after.medianTruthErrMs} ms, was ${row.before.medianTruthErrMs}`
		);
		assert.ok(row.after.medianTruthErrMs < 3, `${name}: audio off by ${row.after.medianTruthErrMs} ms`);
		assert.ok(row.after.p95TruthErrMs < 5, `${name}: audio p95 off by ${row.after.p95TruthErrMs} ms`);
		console.log(
			`# ${name}: re-joins/min ${row.before.reseeksPerMin.toFixed(1)} -> ${row.after.reseeksPerMin.toFixed(1)}, ` +
				`trimming ${(row.before.trimTimeFrac * 100).toFixed(0)}% -> ${(row.after.trimTimeFrac * 100).toFixed(1)}%, ` +
				`|audio error| median ${row.before.medianTruthErrMs.toFixed(2)} -> ${row.after.medianTruthErrMs.toFixed(2)} ` +
				`p95 ${row.before.p95TruthErrMs.toFixed(1)} -> ${row.after.p95TruthErrMs.toFixed(1)} ms`
		);
	}
	const col = (side, key) => median(rows.map((row) => row[side][key]));
	assert.ok(col('before', 'reseeksPerMin') > 2, 'baseline re-joins per minute');
	console.log(
		`# jittered steady (n=${rows.length}): re-joins/min ${col('before', 'reseeksPerMin').toFixed(1)} -> ` +
			`${col('after', 'reseeksPerMin').toFixed(1)}; time trimming ` +
			`${(col('before', 'trimTimeFrac') * 100).toFixed(1)}% -> ${(col('after', 'trimTimeFrac') * 100).toFixed(1)}%; ` +
			`median |audio error| ${col('before', 'medianTruthErrMs').toFixed(2)} -> ` +
			`${col('after', 'medianTruthErrMs').toFixed(2)} ms`
	);
});

test('smoothedBeatTimes pulls a dithered grid toward the real beats and leaves the count alone', () => {
	for (const { stored, truth } of JITTERED) {
		const smooth = pl.smoothedBeatTimes(stored);
		assert.equal(smooth.length, stored.length);
		const rms = (value) => Math.sqrt(truth.reduce((sum, beat, i) => sum + (value(i) - beat.t) ** 2, 0) / truth.length);
		const storedRms = rms((i) => stored[i].t);
		const smoothRms = rms((i) => smooth[i]);
		assert.ok(smoothRms < storedRms / 2, `rms off the real beat ${smoothRms} against ${storedRms} stored`);
		for (let i = 1; i < smooth.length; i++) assert.ok(smooth[i] > smooth[i - 1], `beat ${i} not after beat ${i - 1}`);
	}
	assert.throws(() => pl.smoothedBeatTimes(EVEN[0], 0), /half window/);
});

// ------------------------------------------------------ even grids: control

test('control: an even grid is not re-joined or trimmed, before or after', () => {
	for (const stored of EVEN) {
		for (const run of [beforeChange, after]) {
			const result = run({ followerBeats: stored });
			assert.equal(result.reseeks, 0);
			assert.equal(result.trimTimeFrac, 0);
			assert.equal(result.baseMovesPerMin, 0, 'the base tempo of an even pair never moves');
		}
	}
});

test('control: on even grids the measure, the trim and the tempo are the legacy ones, bit for bit', () => {
	const random = lcg(7);
	const master = EVEN[2];
	let compared = 0;
	for (const follower of EVEN) {
		for (let trial = 0; trial < 400; trial++) {
			const masterSec = 5 + random() * 200;
			// Anywhere within +-40 ms of in phase, and sometimes anywhere at all.
			const m = (masterSec - master[0].t) / (master[1].t - master[0].t);
			const spb = follower[1].t - follower[0].t;
			const offsetSec = trial % 5 === 0 ? random() * spb : (random() - 0.5) * 0.08;
			const followerSec = follower[0].t + m * spb + offsetSec;
			const input = decisionInput(master, follower, masterSec, followerSec, {
				followerBaseTempo: 0.9 + random() * 0.2,
				trimming: random() < 0.5,
				normalization: [1, 1, 0.5, 2][trial % 4],
				// Past both re-join gates, which is where the legacy lock always was.
				overLineTicks: pl.PHASE_LOCK_REJOIN_CONFIRM_TICKS - 1,
				sinceJoinSec: pl.PHASE_LOCK_REJOIN_MIN_INTERVAL_SEC
			});
			const now = pl.phaseLockDecision(input);
			const was = legacyPhaseLockDecision(input);
			assert.equal(now.action, was.action);
			assert.equal(now.tempo, was.tempo);
			assert.equal(now.errorMs, was.errorMs);
			assert.equal(now.base, input.followerBaseTempo, 'no tempo follow on an even pair');
			compared += 1;
		}
	}
	assert.equal(compared, EVEN.length * 400);
});

// ----------------------------------------------------- real tempo changes

for (const [label, bpmAt, changeEndsAtBeat, endBpm] of [
	['ramp of 2.5% over 32 beats', rampBpm, RAMP_BEATS.to, 123],
	['step of 1.7% in one beat', stepBpm, STEP_BEAT, 122]
]) {
	test(`a real tempo change is followed, without a seek: ${label}`, () => {
		const { stored, truth, master } = variableTempo(bpmAt, 31);
		const now = after({ followerBeats: stored, truthBeats: truth, masterBeats: master, trace: true });
		assert.equal(now.reseeks, 0, 'followed by a tempo move, not a seek');
		// The follower's native tempo ends above the master's, so it is slowed.
		const wantedTempo = MASTER_BPM / endBpm;
		const settleBeats = pl.PHASE_LOCK_SMOOTH_HALF_WINDOW_BEATS + 1;
		const settledAtSec = master[changeEndsAtBeat + settleBeats].t;
		const settled = now.trace.filter((tick) => tick.masterSec >= settledAtSec);
		assert.ok(settled.length > 1000, 'the track goes on long enough to judge');
		for (const tick of settled) {
			assert.ok(
				Math.abs(tick.base / wantedTempo - 1) < 0.002,
				`base ${tick.base} at ${tick.masterSec.toFixed(1)} s, wanted ${wantedTempo} within 0.2%`
			);
		}
		assert.ok(median(settled.map((tick) => Math.abs(tick.truthMs))) < 3, 'the audio is back on the beat');
		const worst = Math.max(...now.trace.map((tick) => Math.abs(tick.truthMs)));
		assert.ok(worst < 25, `the audio is never more than ${worst.toFixed(1)} ms off through the change`);
		// What the change does to the lock as it was: it cannot follow, so it seeks.
		const was = beforeChange({ followerBeats: stored, truthBeats: truth, masterBeats: master });
		assert.ok(was.reseeks > 0, 'the legacy lock re-joins on this grid (else nothing is under test)');
		console.log(
			`# ${label}: re-joins ${was.reseeks} -> ${now.reseeks}; time trimming ` +
				`${(was.trimTimeFrac * 100).toFixed(1)}% -> ${(now.trimTimeFrac * 100).toFixed(1)}%; ` +
				`median |audio error| ${was.medianTruthErrMs.toFixed(2)} -> ${now.medianTruthErrMs.toFixed(2)} ms; ` +
				`worst through the change ${worst.toFixed(1)} ms`
		);
	});
}

test('the base follows the local tempo only with an uneven grid, and stays in the pitch range', () => {
	const { stored } = JITTERED[0];
	const period = (stored[stored.length - 1].t - stored[0].t) / (stored.length - 1);
	const master = evenGrid(period, 600);
	const at = (extra) =>
		pl.phaseLockDecision(decisionInput(master, stored, master[100].t + 0.01, stored[100].t + 0.01, extra));
	// A base 1% off the grids' own ratio is moved to it.
	const moved = at({ followerBaseTempo: 1.01 });
	assert.ok(Math.abs(moved.base - 1) < 0.002, `base moved to ${moved.base}`);
	// Inside the follow threshold it is left alone, exactly; past it, moved.
	// Literal ratios (0.02% and 0.1%), so a threshold of zero or of percents fails here.
	const near = moved.base * 1.0002;
	assert.equal(at({ followerBaseTempo: near }).base, near);
	assert.equal(at({ followerBaseTempo: moved.base * 1.001 }).base, moved.base);
	// And on steady music the base is retuned a few times a minute, not every tick.
	const steady = after({ followerBeats: stored });
	console.log(`# steady jittered grid: base moves ${steady.baseMovesPerMin.toFixed(1)}/min`);
	assert.ok(steady.baseMovesPerMin < 120, `base moved ${steady.baseMovesPerMin}/min on steady music`);
	// Two follower beats per master beat: twice the tempo, not half.
	const double = at({ followerBaseTempo: 1, normalization: 2 });
	assert.equal(double.base, 1.08, 'a 2x local tempo is held at the top of the +-8% range');
	const half = pl.phaseLockDecision(
		decisionInput(evenGrid(period / 2, 1200), stored, 20, stored[40].t, { normalization: 0.5, followerBaseTempo: 1.01 })
	);
	assert.ok(Math.abs(half.base - 1) < 0.002, `half-time lock base ${half.base}`);
});

// ------------------------------------------------------- the DJ's own offset

test('an offset the DJ dialed in is held: not trimmed back, not re-joined', () => {
	for (const [name, follower] of [['even', EVEN[1]], ['jittered', JITTERED[1].stored]]) {
		for (const displaceMs of [8, -8, 30]) {
			const events = [{ atSec: 20, displaceMs, registered: true }];
			const held = after({ followerBeats: follower, events, trace: true });
			const rest = held.trace.filter((tick) => tick.masterSec > 21);
			assert.equal(held.reseeks, 0, `${name} ${displaceMs} ms: not re-joined`);
			if (name === 'even') {
				assert.ok(rest.every((tick) => tick.action === 'base'), `${name} ${displaceMs} ms: never trimmed`);
			}
			const standing = median(rest.map((tick) => tick.storedMs));
			assert.ok(Math.abs(standing - displaceMs) < 3, `${name}: stands ${standing.toFixed(1)} ms off, dialed ${displaceMs}`);
			// The same displacement the DJ did NOT dial in is an error, and is corrected.
			const corrected = after({ followerBeats: follower, events: [{ ...events[0], registered: false }], trace: true });
			const end = corrected.trace.slice(-300);
			assert.ok(
				Math.abs(median(end.map((tick) => tick.storedMs))) < 3,
				`${name} ${displaceMs} ms unregistered: pulled back onto the beat`
			);
			assert.ok(corrected.trimTimeFrac > 0 || corrected.reseeks > 0, `${name}: the lock acted on it`);
		}
	}
});

test('the held offset wraps like the phase does, and a malformed one throws', () => {
	const master = EVEN[2];
	const beatMs = (master[1].t - master[0].t) * 1000;
	const input = (userOffsetMs) => decisionInput(master, master, 60, 60 + 0.004, { userOffsetMs });
	assert.ok(Math.abs(pl.phaseErrorMs(input(4)) - 0) < 1e-6);
	assert.ok(Math.abs(pl.phaseErrorMs(input(4 + beatMs)) - 0) < 1e-6, 'a whole beat of offset is no offset');
	assert.ok(Math.abs(pl.phaseErrorMs(input(0)) - 4) < 1e-6);
	assert.throws(() => pl.phaseLockDecision(input(Number.NaN)), /user offset/);
});

// --------------------------------------------- re-joins: rare and deliberate

test('a genuine large offset is re-joined exactly once', () => {
	for (const [name, follower] of [['even', EVEN[1]], ['jittered', JITTERED[1].stored]]) {
		for (const displaceMs of [60, -60, 140]) {
			const result = after({ followerBeats: follower, events: [{ atSec: 30, displaceMs, registered: false }], trace: true });
			assert.equal(result.reseeks, 1, `${name} ${displaceMs} ms: one re-join, got ${result.reseeks}`);
			const at = result.trace.find((tick) => tick.action === 'reseek');
			assert.ok(at.masterSec - 30 < 0.2, `${name}: re-joined ${(at.masterSec - 30).toFixed(2)} s after the offset`);
			const end = result.trace.slice(-300);
			assert.ok(Math.abs(median(end.map((tick) => tick.lockMs))) < 2, `${name}: back in phase`);
		}
	}
});

test('an offset the cap can walk back before the next re-join is allowed is trimmed, not seeked', () => {
	// 20 ms, two seconds after a join: past the 15 ms line, inside the minimum interval.
	const events = [{ atSec: 2, displaceMs: 20, registered: false }];
	const now = after({ followerBeats: EVEN[1], events, trace: true });
	assert.equal(now.reseeks, 0);
	assert.ok(now.capTimeFrac > 0, 'the trim ran at its cap');
	const sixSecondsOn = now.trace.find((tick) => tick.masterSec >= 8);
	assert.ok(Math.abs(sixSecondsOn.lockMs) < 5, `error ${sixSecondsOn.lockMs} ms six seconds on`);
	assert.equal(beforeChange({ followerBeats: EVEN[1], events }).reseeks, 1, 'the legacy lock seeks here');
});

test('re-join gates: confirmed over consecutive ticks, and never inside the minimum interval', () => {
	const master = EVEN[2];
	const off = (ms, extra) => pl.phaseLockDecision(decisionInput(master, master, 60, 60 + ms / 1000, extra));
	const confirm = pl.PHASE_LOCK_REJOIN_CONFIRM_TICKS;
	const interval = pl.PHASE_LOCK_REJOIN_MIN_INTERVAL_SEC;
	// One reading past the line never seeks, however old the join.
	assert.equal(off(40, { overLineTicks: 0 }).action, 'trim');
	// Past the line, tick by tick.
	let ticks = 0;
	for (let tick = 1; tick < confirm; tick++) {
		const d = off(40, { overLineTicks: ticks });
		assert.equal(d.action, 'trim', `tick ${tick} past the line is still a trim`);
		assert.equal(d.overLineTicks, tick);
		assert.ok(Math.abs(d.tempo - (1 - pl.PHASE_LOCK_MAX_TRIM)) < 1e-12, 'at the cap meanwhile');
		ticks = d.overLineTicks;
	}
	assert.equal(off(40, { overLineTicks: ticks }).action, 'reseek', 'the confirming tick re-joins');
	// One tick back under the line starts the count again.
	assert.equal(off(10, { overLineTicks: ticks }).overLineTicks, 0);
	assert.equal(off(0, { overLineTicks: ticks }).overLineTicks, 0);
	// The minimum interval, on both sides of its edge.
	const confirmed = { overLineTicks: confirm - 1 };
	assert.equal(off(40, { ...confirmed, sinceJoinSec: interval - 0.01 }).action, 'trim');
	assert.equal(off(40, { ...confirmed, sinceJoinSec: interval }).action, 'reseek');
	// It keeps counting while it waits, so the re-join comes the tick the interval ends.
	assert.equal(off(40, { overLineTicks: 50, sinceJoinSec: 1 }).overLineTicks, 51);
	// The interval is long enough for the cap to walk back the whole re-join line.
	assert.ok(
		pl.PHASE_LOCK_MAX_TRIM * interval * 1000 >= pl.PHASE_LOCK_RESEEK_MS,
		'an error at the line is recoverable by the capped trim inside the interval'
	);
});

// ----------------------------------------------------------- the grid cache

test('a grid edited in place is re-read, not served from the cache', () => {
	const master = evenGrid(0.5, 400);
	const follower = evenGrid(0.5, 400).map((beat) => ({ ...beat }));
	const input = decisionInput(master, follower, 50.1, 50.1);
	assert.ok(Math.abs(pl.phaseErrorMs(input)) < 1e-6);
	for (const beat of follower) beat.t += 0.006; // the whole grid shifted 6 ms later
	assert.ok(Math.abs(pl.phaseErrorMs(input) + 6) < 1e-6, `shifted grid reads ${pl.phaseErrorMs(input)} ms`);
	// And an even grid that BECOMES uneven is smoothed from then on.
	const stored = JITTERED[0].stored;
	for (let i = 0; i < follower.length; i++) follower[i].t = stored[i].t;
	const smoothedMs = pl.phaseErrorMs(decisionInput(master, follower, 50.1, stored[100].t));
	const storedMs = storedPhaseErrorMs(decisionInput(master, follower, 50.1, stored[100].t));
	assert.notEqual(smoothedMs, storedMs);
});
