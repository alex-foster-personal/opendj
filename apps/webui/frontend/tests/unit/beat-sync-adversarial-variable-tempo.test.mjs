/**
 * ADVERSARIAL (round 1, intentionally RED until fixed): Beat Sync never
 * follows a tempo change INSIDE a track's grid. The follower keeps the base
 * tempo its join chose, the phase lock can only trim 0.3% around it, and any
 * larger change in either deck's grid tempo turns into repeated re-seeks
 * (audible jumps) until a re-join happens to land past the change.
 *
 * Where: the base is fixed at join time (`_synchronizeFollowers` ->
 * `_phaseLock.record(..., base: plan.followerTempoRatio)` in
 * audio-engine.svelte.ts ~line 2656; `_join` in rust-transport.ts ~line 125),
 * and `phaseLockDecision` (src/lib/rb/phase-lock.ts) clamps the correction to
 * base * (1 +- PHASE_LOCK_MAX_TRIM). Nothing re-plans the base when the local
 * grid tempo moves (no caller of gridBpmAt / _windowedIntervalBpm on the tick).
 * On top of that, the join's tempo is a +-32-beat window MEAN
 * (`_windowedIntervalBpm`, beat-sync-math.ts:225), so a re-join near a
 * change picks a blended tempo that is wrong on both sides of it.
 *
 * Grids: rekordbox dynamic analysis stores several const_regions; live
 * drums, older disco/funk, and edits with a tempo change produce exactly this.
 *
 * Fix direction (the passing control below models it): feed the lock a
 * feed-forward base from the LOCAL beat intervals at both playheads on each
 * tick (masterLocalBpm * masterTempo * normalization / followerLocalBpm) and
 * trim only the residual.
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

function rampGrid(fromBpm, toBpm, steadyBeats, rampBeats, total) {
	const beats = [];
	let t = 0.1;
	for (let i = 0; i < total; i++) {
		const k = Math.min(Math.max(i - steadyBeats, 0), rampBeats) / rampBeats;
		const bpm = fromBpm + k * (toBpm - fromBpm);
		beats.push({ n: (i % 4) + 1, bpm, t });
		t += 60 / bpm;
	}
	return beats;
}

function stepGrid(fromBpm, toBpm, atBeat, total) {
	const beats = [];
	let t = 0.1;
	for (let i = 0; i < total; i++) {
		const bpm = i < atBeat ? fromBpm : toBpm;
		beats.push({ n: (i % 4) + 1, bpm, t });
		t += 60 / bpm;
	}
	return beats;
}

/** Local tempo at a position: 60 / the enclosing beat interval. */
function localBpm(beats, sec) {
	const b = pl.gridBeatPosition(beats, sec);
	const i = Math.floor(b);
	return 60 / (beats[i + 1].t - beats[i].t);
}

/**
 * Closed loop at 30 Hz. A re-seek is modeled as the real engines do it: a
 * fresh join (`computeFollowerSyncPlan`) that resets position AND base.
 * `feedForward` models the fix direction.
 */
function run(masterGrid, followerGrid, seconds, feedForward = false) {
	const tick = 1 / 30;
	let m = 20;
	let f = 20;
	let base = 1;
	const rejoin = () => {
		const p = bsm.computeFollowerSyncPlan({
			masterGrid,
			followerGrid,
			masterPositionAtSyncSec: m,
			masterTempoRatio: 1,
			followerPositionSec: f,
			currentContextTimeSec: 0,
			syncAtContextTimeSec: 0.01,
			minFollowerTempoRatio: 0.92,
			maxFollowerTempoRatio: 1.08,
			mode: 'beat'
		});
		f = p.followerPositionSec;
		base = p.followerTempoRatio;
	};
	rejoin();
	let sent = base;
	let reseeks = 0;
	let flamSec = 0;
	for (let k = 0; k < seconds * 30; k++) {
		const effectiveBase = feedForward ? localBpm(masterGrid, m) / localBpm(followerGrid, f) : base;
		const d = pl.phaseLockDecision({
			masterBeats: masterGrid,
			masterPositionSec: m,
			masterTempo: 1,
			followerBeats: followerGrid,
			followerPositionSec: f,
			followerBaseTempo: effectiveBase,
			pitchRangePct: 8,
			trimming: sent !== effectiveBase
		});
		if (d.action === 'unmeasured') break;
		if (d.action === 'reseek') {
			reseeks += 1;
			rejoin();
			sent = base;
			continue;
		}
		if (Math.abs(d.errorMs) >= 10) flamSec += tick;
		if (feedForward || pl.phaseLockShouldSend(sent, d.tempo, effectiveBase)) sent = d.tempo;
		m += tick;
		f += tick * sent;
	}
	return { reseeks, flamSec };
}

test('master grid ramps 124 -> 128 BPM over 16 bars: the follower must not be re-seeked', () => {
	const master = rampGrid(124, 128, 40, 64, 1500);
	const r = run(master, grid(126, 0.05, 1500), 120);
	// Observed on the unfixed code: 2 re-seeks and ~27 s of >= 10 ms flam
	// (follower step case: 2 re-seeks, ~31 s).
	assert.equal(r.reseeks, 0, `${r.reseeks} audible re-seeks, ${r.flamSec.toFixed(1)} s of flam`);
	assert.ok(r.flamSec < 1, `${r.flamSec.toFixed(1)} s of flam`);
});

test('follower grid steps 126 -> 130 BPM (two const_regions): no re-seek cascade', () => {
	const follower = stepGrid(126, 130, 60, 1500);
	const r = run(grid(128, 0.1, 1500), follower, 120);
	assert.equal(r.reseeks, 0, `${r.reseeks} audible re-seeks, ${r.flamSec.toFixed(1)} s of flam`);
	assert.ok(r.flamSec < 1, `${r.flamSec.toFixed(1)} s of flam`);
});

test('fix-direction control: a feed-forward local-tempo base holds both cases with no re-seek', () => {
	for (const [master, follower] of [
		[rampGrid(124, 128, 40, 64, 1500), grid(126, 0.05, 1500)],
		[grid(128, 0.1, 1500), stepGrid(126, 130, 60, 1500)]
	]) {
		const r = run(master, follower, 120, true);
		assert.equal(r.reseeks, 0);
		assert.ok(r.flamSec < 1, `${r.flamSec}`);
	}
});

test('control: constant grids never re-seek under the same loop (the harness is not the cause)', () => {
	const r = run(grid(128, 0.1, 1500), grid(126, 0.05, 1500), 120);
	assert.equal(r.reseeks, 0);
	assert.equal(r.flamSec, 0);
});
