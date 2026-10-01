/**
 * ADVERSARIAL (round 1; was RED, FIXED by the phase-lock.ts wrap-unit change):
 * the continuous phase lock (NAE-19) measured a FALSE half-master-beat error on a correctly joined
 * double-tempo follower, so it re-seeks a deck that is already in phase.
 *
 * Setup a DJ hits: master at ~87 BPM (half-time hip-hop, or any track
 * rekordbox analyzed at half tempo) and a ~174 BPM follower. The join
 * (`computeFollowerSyncPlan`) folds with tempoNormalization 2: two follower
 * beats per master beat, follower at ratio 1. It may anchor on an EVEN or
 * an ODD follower beat - both put a follower beat on every master beat, both
 * are musically in phase.
 *
 * `phaseErrorMs` (src/lib/rb/phase-lock.ts) computes
 *   diff = f / normalization - m
 * and wraps `diff` to the nearest WHOLE master beat. With normalization 2 an
 * odd follower anchor makes f / 2 carry an extra 0.5, so a perfectly aligned
 * pair reads as half a master beat off (~345 ms at 87 BPM), past
 * PHASE_LOCK_RESEEK_BEATS (0.25), and the lock asks for a re-seek. The
 * re-join lands on the same odd lineage (it is the nearest anchor), so the
 * lock re-seeks again on the next settled tick: a seek loop instead of a lock.
 *
 * Ground truth is checked independently of the phase lock: at every master
 * beat in the next 16, the follower sits on one of its own beat timestamps.
 *
 * Fix direction (verified by mutation, see findings.md): wrap in units of
 * min(1, 1 / normalization) master beats, i.e. for normalization 2 wrap
 * (f - 2m) to the nearest whole FOLLOWER beat. With that change this file
 * goes green and the even-anchor and normalization-0.5 controls stay green.
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

function grid(bpm, startSec, count) {
	const spb = 60 / bpm;
	return Array.from({ length: count }, (_, i) => ({ n: (i % 4) + 1, bpm, t: startSec + i * spb }));
}

const MASTER = grid(87, 0.2, 800);
const FOLLOWER = grid(174, 0.1, 1600);
const MASTER_AT = 60.123;

function plan(followerPositionSec, mode) {
	return bsm.computeFollowerSyncPlan({
		masterGrid: MASTER,
		followerGrid: FOLLOWER,
		masterPositionAtSyncSec: MASTER_AT,
		masterTempoRatio: 1,
		followerPositionSec,
		currentContextTimeSec: 0,
		syncAtContextTimeSec: 0.1,
		minFollowerTempoRatio: 0.92,
		maxFollowerTempoRatio: 1.08,
		mode
	});
}

/** Ground truth: every master beat in the next 16 meets a follower beat. */
function assertMusicallyInPhase(p) {
	const mStart = pl.gridBeatPosition(MASTER, MASTER_AT);
	for (let k = 1; k <= 16; k++) {
		const masterBeatSec = MASTER[Math.floor(mStart) + k].t;
		const elapsedSec = masterBeatSec - MASTER_AT; // master tempo 1
		const followerSec = p.followerPositionSec + elapsedSec * p.followerTempoRatio;
		const f = pl.gridBeatPosition(FOLLOWER, followerSec);
		const offBeat = Math.abs(f - Math.round(f));
		assert.ok(offBeat < 1e-6, `master beat +${k}: follower ${offBeat} beats off its own grid`);
	}
}

function lockDecision(p) {
	return pl.phaseLockDecision({
		masterBeats: MASTER,
		masterPositionSec: MASTER_AT,
		masterTempo: 1,
		followerBeats: FOLLOWER,
		followerPositionSec: p.followerPositionSec,
		followerBaseTempo: p.followerTempoRatio,
		normalization: p.tempoNormalization,
		pitchRangePct: 8,
		sinceJoinSec: 600,
		// One tick short of a confirmed re-join: a wrong measure re-seeks HERE.
		overLineTicks: pl.PHASE_LOCK_REJOIN_CONFIRM_TICKS - 1
	});
}

for (const mode of ['beat', 'bar']) {
	test(`${mode}: an ODD-anchored double-tempo join is in phase and the lock must leave it alone`, () => {
		const p = plan(30, mode);
		assert.equal(p.tempoNormalization, 2, 'precondition: the fold is double tempo');
		assert.equal(p.followerBeatIndex % 2, 1, 'precondition: the join anchored on an odd follower beat');
		assertMusicallyInPhase(p);
		const d = lockDecision(p);
		// Observed on the unfixed code: action 'reseek', errorMs ~ -344.8
		// (exactly half an 87 BPM beat), for a follower that is in phase.
		assert.equal(
			d.action,
			'base',
			`phase lock read ${d.errorMs?.toFixed(1)} ms on an in-phase follower and chose '${d.action}'`
		);
		assert.ok(Math.abs(d.errorMs) < 0.5, `errorMs ${d.errorMs}`);
	});
}

test('control: an EVEN-anchored double-tempo join reads zero error (the bug is parity, not the fold)', () => {
	const p = plan(30.35, 'beat');
	assert.equal(p.tempoNormalization, 2);
	assert.equal(p.followerBeatIndex % 2, 0);
	assertMusicallyInPhase(p);
	const d = lockDecision(p);
	assert.equal(d.action, 'base');
	assert.ok(Math.abs(d.errorMs) < 0.5);
});

test('control: a REAL half-follower-beat offset on a double-tempo pair is still caught', () => {
	// Overshoot guard for the fix: shifting the follower by half of ITS beat
	// puts its beats between master beats - a real flam the lock must see.
	const p = plan(30.35, 'beat');
	const halfFollowerBeat = 0.5 * (60 / 174);
	const d = lockDecision({ ...p, followerPositionSec: p.followerPositionSec + halfFollowerBeat });
	assert.notEqual(d.action, 'base', 'a quarter-master-beat flam must not read as in phase');
	assert.ok(Math.abs(Math.abs(d.errorMs) - 172.4) < 1, `errorMs ${d.errorMs}`);
});

test('control: half tempo (normalization 0.5) is measured correctly on either parity', () => {
	const m174 = grid(174, 0.1, 1600);
	const f87 = grid(87, 0.2, 800);
	for (const masterAt of [60.0, 60.123, 60.4]) {
		const p = bsm.computeFollowerSyncPlan({
			masterGrid: m174,
			followerGrid: f87,
			masterPositionAtSyncSec: masterAt,
			masterTempoRatio: 1,
			followerPositionSec: 30,
			currentContextTimeSec: 0,
			syncAtContextTimeSec: 0.1,
			minFollowerTempoRatio: 0.92,
			maxFollowerTempoRatio: 1.08,
			mode: 'beat'
		});
		assert.equal(p.tempoNormalization, 0.5);
		const d = pl.phaseLockDecision({
			masterBeats: m174,
			masterPositionSec: masterAt,
			masterTempo: 1,
			followerBeats: f87,
			followerPositionSec: p.followerPositionSec,
			followerBaseTempo: p.followerTempoRatio,
			normalization: 0.5,
			pitchRangePct: 8,
			sinceJoinSec: 600,
			overLineTicks: pl.PHASE_LOCK_REJOIN_CONFIRM_TICKS - 1
		});
		assert.equal(d.action, 'base', `master at ${masterAt}: ${d.errorMs}`);
	}
});

test('control: at normalization 1 a half-beat offset still reads half a beat (the fix only widens N=2)', () => {
	// Overshoot guard: wrapping EVERY pair in half-beat units would make a
	// follower playing on the master's off-beats read as in phase.
	const m128 = grid(128, 0.1, 1200);
	const f126 = grid(126, 0.05, 1200);
	const mb = pl.gridBeatPosition(m128, 60);
	const i = Math.floor(mb);
	const offBeat = f126[i].t + (mb - i + 0.5) * (f126[i + 1].t - f126[i].t);
	const error = pl.phaseErrorMs({
		masterBeats: m128,
		masterPositionSec: 60,
		masterTempo: 1,
		followerBeats: f126,
		followerPositionSec: offBeat,
		followerBaseTempo: 128 / 126,
		normalization: 1,
		pitchRangePct: 8
	});
	assert.ok(Math.abs(Math.abs(error) - 234.375) < 0.5, `off-beat follower reads ${error} ms`);
});
