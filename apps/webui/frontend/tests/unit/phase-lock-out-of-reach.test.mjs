/**
 * Phase lock OUT OF REACH (BEATSYNC-REACH-8872): a follower whose grid's local tempo
 * leaves the pitch range must play ON through that stretch, not be re-joined
 * somewhere else in its track.
 *
 * Found on a real pair (Tue 3 Nov 2026, AutoPlay long blend): a 93.75 BPM
 * master against a live-band follower whose rekordbox dynamic grid runs about
 * 168 BPM, then a free intro stretch near 120 BPM, then 169. Folded to double
 * time at a 16 percent range, the intro needs a ratio near 1.55. The lock held
 * the base at the range edge (right), the error grew past the re-join line,
 * and the re-join (`computeFollowerSyncPlan`) found the NEAREST beat whose
 * tempo the range can reach: about 9 s back, at the end of the 168 BPM part.
 * Every 8 s, again: the closed loop on the real grids made 10 re-joins and
 * 83 s of backward jumps in an 82 s blend, and the follower never got past its
 * intro to its bass. Now a re-join out of reach is taken only when it stays
 * within `PHASE_LOCK_REJOIN_MAX_JUMP_BEATS` of the follower.
 *
 * Requirements:
 *   [if] an out-of-reach follower is re-joined more than 2 of its beats away
 *   from where it plays [then] broken
 *   [if] a follower that leaves reach and comes back does not end the blend
 *   past the stretch, phase-locked again [then] broken
 *   [if] an in-reach follower past the re-join line is no longer re-joined
 *   [then] broken (control: the over-correction "never re-join")
 *   [if] an out-of-reach follower with a reachable beat right beside it is no
 *   longer re-joined [then] broken (control)
 *
 * Fixtures are synthetic: no library track is stored here.
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

/** Beats at a tempo curve, stored to 10 ms like a rekordbox dynamic grid. */
function curveGrid(bpmAt, count, startSec = 0.3) {
	const beats = [];
	let t = startSec;
	for (let i = 0; i < count; i++) {
		beats.push({ n: (i % 4) + 1, bpm: bpmAt(i), t: Math.round(t * 100) / 100 });
		t += 60 / bpmAt(i);
	}
	return beats;
}

const MASTER = () => curveGrid(() => 93.75, 700, 0.2);
/** 168 BPM for 40 beats, a 120 BPM stretch for 90, then 169. */
const LIVE_BAND = () => curveGrid((i) => (i < 40 ? 168 : i < 130 ? 120 : 169), 600);

/** Closed loop at 30 Hz as both engines run it; a re-seek is a fresh join. */
function run(masterGrid, followerGrid, { masterStart, followerStart, seconds, range = 16, keyLocked = false }) {
	const tick = 1 / 30;
	let m = masterStart;
	let f = followerStart;
	let base = 1;
	let normalization = 1;
	const rejoin = () => {
		const p = bsm.computeFollowerSyncPlan({
			masterGrid, followerGrid, masterPositionAtSyncSec: m, masterTempoRatio: 1, followerPositionSec: f,
			currentContextTimeSec: 0, syncAtContextTimeSec: 0.01,
			minFollowerTempoRatio: 1 - range / 100, maxFollowerTempoRatio: 1 + range / 100, mode: 'beat'
		});
		f = p.followerPositionSec;
		base = p.followerTempoRatio;
		normalization = p.tempoNormalization;
	};
	rejoin();
	let sent = base;
	let sinceJoinSec = 0;
	let overLineTicks = 0;
	let reseeks = 0;
	let worstJumpBeats = 0;
	const lateErrors = [];
	for (let k = 0; k < seconds * 30; k++) {
		const input = {
			masterBeats: masterGrid, masterPositionSec: m, masterTempo: 1, followerBeats: followerGrid,
			followerPositionSec: f, followerBaseTempo: base, normalization, pitchRangePct: range, keyLocked,
			sinceJoinSec, overLineTicks
		};
		base = pl.phaseLockFeedForwardBase(input);
		const d = pl.phaseLockDecision({ ...input, followerBaseTempo: base, trimming: sent !== base });
		base = d.base;
		overLineTicks = d.overLineTicks;
		sinceJoinSec += tick;
		if (k > (seconds - 10) * 30 && d.errorMs !== null) lateErrors.push(Math.abs(d.errorMs));
		if (d.action === 'reseek') {
			reseeks += 1;
			const from = pl.gridBeatPosition(followerGrid, f);
			rejoin();
			worstJumpBeats = Math.max(worstJumpBeats, Math.abs(pl.gridBeatPosition(followerGrid, f) - from));
			sent = base;
			sinceJoinSec = 0;
			overLineTicks = 0;
		} else if (pl.phaseLockShouldSend(sent, d.tempo, base)) sent = d.tempo;
		m += tick;
		f += tick * sent;
	}
	lateErrors.sort((a, b) => a - b);
	return { reseeks, worstJumpBeats, followerSec: f, lateP95Ms: lateErrors[Math.floor(lateErrors.length * 0.95)] };
}

test('a follower out of reach plays on through the stretch instead of being re-joined elsewhere', () => {
	const follower = LIVE_BAND();
	const r = run(MASTER(), follower, { masterStart: 30, followerStart: follower[8].t, seconds: 90 });
	// Before: 6 re-joins, each about 8 follower beats back; the follower ended inside the stretch.
	// The anchor is within the radius; a double-time phase point sits up to 2 intervals past it.
	assert.ok(r.worstJumpBeats <= pl.PHASE_LOCK_REJOIN_MAX_JUMP_BEATS + 2, `a re-join moved it ${r.worstJumpBeats.toFixed(1)} beats`);
	assert.ok(r.followerSec > follower[130].t + 20, `the follower ended at ${r.followerSec.toFixed(1)} s, inside its stretch`);
	assert.ok(r.lateP95Ms < 10, `back in reach it is not phase-locked again: p95 ${r.lateP95Ms} ms`);
});

test('control: key-locked, the same follower plays through too (no re-join needed)', () => {
	const follower = LIVE_BAND();
	const r = run(MASTER(), follower, { masterStart: 30, followerStart: follower[8].t, seconds: 90, keyLocked: true });
	assert.ok(r.followerSec > follower[130].t + 20);
	assert.ok(r.lateP95Ms < 10, `p95 ${r.lateP95Ms} ms`);
});

test('control: an in-reach follower past the re-join line is still re-joined', () => {
	const grid = curveGrid(() => 126, 800);
	const input = {
		masterBeats: curveGrid(() => 128, 800), masterPositionSec: 60, masterTempo: 1, followerBeats: grid,
		followerPositionSec: 59.1, followerBaseTempo: 128 / 126, normalization: 1, pitchRangePct: 8,
		sinceJoinSec: 20, overLineTicks: pl.PHASE_LOCK_REJOIN_CONFIRM_TICKS
	};
	assert.equal(pl.phaseLockDecision(input).action, 'reseek');
});

test('control: out of reach with a reachable beat right beside it, the re-join is still taken', () => {
	// A 120 BPM stretch only 3 beats long: the smoothed local tempo is pulled
	// out of reach there, but the join can anchor on a 168 BPM beat beside it.
	const follower = curveGrid((i) => (i >= 200 && i < 203 ? 120 : 168), 600);
	const masterBeats = MASTER();
	const near = {
		masterBeats, masterPositionSec: 100, masterTempo: 1, followerBeats: follower,
		followerPositionSec: follower[201].t + 0.12, followerBaseTempo: 187.5 / 168, normalization: 2,
		pitchRangePct: 16, sinceJoinSec: 20, overLineTicks: pl.PHASE_LOCK_REJOIN_CONFIRM_TICKS
	};
	assert.equal(
		bsm.followerAnchorReachableNear({
			masterGrid: masterBeats, masterPositionSec: 100, masterTempoRatio: 1, followerGrid: follower,
			followerPositionSec: near.followerPositionSec, minFollowerTempoRatio: 0.84, maxFollowerTempoRatio: 1.16, radiusBeats: 2
		}),
		true
	);
	const d = pl.phaseLockDecision(near);
	assert.ok(d.action === 'reseek' || Math.abs(d.errorMs) <= 160, `${d.action} at ${d.errorMs} ms`);
	// And deep inside a long stretch nothing within two beats is reachable.
	const deep = LIVE_BAND();
	assert.equal(
		bsm.followerAnchorReachableNear({
			masterGrid: masterBeats, masterPositionSec: 100, masterTempoRatio: 1, followerGrid: deep,
			followerPositionSec: deep[85].t, minFollowerTempoRatio: 0.84, maxFollowerTempoRatio: 1.16, radiusBeats: 2
		}),
		false
	);
});

test('BAR mode: an exact anchor far back is not taken because a folded beat is near (Codex P1 on #8872)', () => {
	// 40 beats at the master's 93.75, 60 at 168, 90 at 120: at follower beat 101
	// the 120 stretch is out of reach, and BAR's join prefers the exact 93.75
	// anchor far back over the folded 168 beats beside it.
	const master = MASTER();
	const follower = curveGrid((i) => (i < 40 ? 93.75 : i < 100 ? 168 : 120), 400);
	const at = follower[101].t + 0.05;
	const input = (syncMode) => ({
		masterBeats: master, masterPositionSec: 100, masterTempo: 1, followerBeats: follower,
		followerPositionSec: at, followerBaseTempo: 1.16, normalization: 2,
		pitchRangePct: 16, sinceJoinSec: 20, overLineTicks: pl.PHASE_LOCK_REJOIN_CONFIRM_TICKS, syncMode
	});
	const barPlan = bsm.computeFollowerSyncPlan({
		masterGrid: master, followerGrid: follower, masterPositionAtSyncSec: 100, masterTempoRatio: 1,
		followerPositionSec: at, currentContextTimeSec: 0, syncAtContextTimeSec: 1,
		minFollowerTempoRatio: 0.84, maxFollowerTempoRatio: 1.16, mode: 'bar'
	});
	assert.ok(at - barPlan.followerPositionSec > 10, `the repro: BAR plans ${barPlan.followerPositionSec} from ${at}`);
	for (const syncMode of ['bar', undefined]) {
		const d = pl.phaseLockDecision(input(syncMode));
		assert.notEqual(d.action, 'reseek', `${syncMode ?? 'unknown'} mode re-joined at ${d.errorMs} ms`);
	}
});
