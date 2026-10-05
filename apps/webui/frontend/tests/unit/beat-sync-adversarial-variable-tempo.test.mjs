/**
 * ADVERSARIAL (round 1; was RED, FIXED by phaseLockFeedForwardBase): Beat Sync never
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
 * Fixed: both engines now take a feed-forward base on every lock tick
 * (`phaseLockFeedForwardBase`, phase-lock.ts): the ratio both grids ask for
 * over four master beats centered on the playheads, moved only past half the
 * trim cap, and the lock trims only the residual. The run() model below is
 * that loop; the SOURCE test pins it to both engines, and
 * phase-lock-webaudio.test.mjs / beat-sync-adversarial-rust-transport.test.mjs
 * drive the real lock bookkeeping across a grid tempo change.
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { readFrontendSource } from './engine-source.mjs';
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

const r3 = (x) => Math.round(x * 1000) / 1000;
/** rekordbox stores beat times to the millisecond: the dither the base must ignore. */
const msRounded = (beats) => beats.map((b) => ({ ...b, t: r3(b.t) }));

/**
 * Closed loop at 30 Hz, as both engines run it: each tick takes the
 * feed-forward base (`phaseLockFeedForwardBase`, pinned to both engines by the
 * SOURCE test below) and then the trim decision on top of it. A re-seek is
 * modeled as the real engines do it: a fresh join (`computeFollowerSyncPlan`)
 * that resets position AND base. `fixedBase` models the unfixed engines.
 */
function run(masterGrid, followerGrid, seconds, { fixedBase = false, masterTempo = 1 } = {}) {
	const tick = 1 / 30;
	let m = 20;
	let f = 20;
	let base = 1;
	let normalization = 1;
	const rejoin = () => {
		const p = bsm.computeFollowerSyncPlan({
			masterGrid,
			followerGrid,
			masterPositionAtSyncSec: m,
			masterTempoRatio: masterTempo,
			followerPositionSec: f,
			currentContextTimeSec: 0,
			syncAtContextTimeSec: 0.01,
			minFollowerTempoRatio: 0.92,
			maxFollowerTempoRatio: 1.08,
			mode: 'beat'
		});
		f = p.followerPositionSec;
		base = p.followerTempoRatio;
		normalization = p.tempoNormalization;
	};
	rejoin();
	let sent = base;
	let reseeks = 0;
	let flamSec = 0;
	let sends = 0;
	let baseMoves = 0;
	// The merged decision refuses a missing join age or a non-integer confirm
	// count, and a re-seek waits for both the confirm ticks and the minimum
	// interval. The loop clock is that age; a re-seek starts it over.
	let sinceJoinSec = 0;
	let overLineTicks = 0;
	for (let k = 0; k < seconds * 30; k++) {
		const input = {
			masterBeats: masterGrid,
			masterPositionSec: m,
			masterTempo,
			followerBeats: followerGrid,
			followerPositionSec: f,
			followerBaseTempo: base,
			normalization,
			pitchRangePct: 8,
			sinceJoinSec,
			overLineTicks
		};
		if (!fixedBase) {
			const next = pl.phaseLockFeedForwardBase(input);
			if (next !== base) baseMoves += 1;
			base = next;
		}
		const d = pl.phaseLockDecision({ ...input, followerBaseTempo: base, trimming: sent !== base });
		overLineTicks = d.overLineTicks;
		sinceJoinSec += tick;
		if (d.action === 'unmeasured') break;
		if (d.action === 'reseek') {
			reseeks += 1;
			rejoin();
			sent = base;
			sinceJoinSec = 0;
			overLineTicks = 0;
			continue;
		}
		if (Math.abs(d.errorMs) >= 10) flamSec += tick;
		if (pl.phaseLockShouldSend(sent, d.tempo, base)) {
			sent = d.tempo;
			sends += 1;
		}
		m += tick * masterTempo;
		f += tick * sent;
	}
	return { reseeks, flamSec, sendsPerSec: sends / seconds, baseMoves };
}

const RAMP = () => rampGrid(124, 128, 40, 64, 1500);
const STEP = () => stepGrid(126, 130, 60, 1500);

test('master grid ramps 124 -> 128 BPM over 16 bars: the follower must not be re-seeked', () => {
	for (const master of [RAMP(), msRounded(RAMP())]) {
		const r = run(master, grid(126, 0.05, 1500), 120);
		// Observed on the unfixed code: 2 re-seeks and ~27 s of >= 10 ms flam
		// (5 re-seeks / ~10.8 s once the 15 ms re-seek line landed).
		assert.equal(r.reseeks, 0, `${r.reseeks} audible re-seeks, ${r.flamSec.toFixed(1)} s of flam`);
		assert.ok(r.flamSec < 1, `${r.flamSec.toFixed(1)} s of flam`);
		assert.ok(r.baseMoves > 0, 'the base followed the ramp');
	}
});

test('follower grid steps 126 -> 130 BPM (two const_regions): no re-seek cascade', () => {
	for (const follower of [STEP(), msRounded(STEP())]) {
		const r = run(grid(128, 0.1, 1500), follower, 120);
		// Observed on the unfixed code: 2 re-seeks, ~31 s (11 / ~10.0 s at 15 ms).
		assert.equal(r.reseeks, 0, `${r.reseeks} audible re-seeks, ${r.flamSec.toFixed(1)} s of flam`);
		assert.ok(r.flamSec < 1, `${r.flamSec.toFixed(1)} s of flam`);
	}
});

test('a master step and a double-tempo follower step are followed too', () => {
	const masterStep = run(stepGrid(128, 124, 80, 1500), grid(126, 0.05, 1500), 120);
	assert.deepEqual([masterStep.reseeks, masterStep.flamSec < 1], [0, true]);
	const double = run(msRounded(grid(87, 0.2, 800)), msRounded(stepGrid(174, 180, 200, 1600)), 120);
	assert.deepEqual([double.reseeks, double.flamSec < 1], [0, true]);
	// At a master tempo other than 1 the base scales with it.
	const pitched = run(grid(128, 0.1, 1500), STEP(), 120, { masterTempo: 1.02 });
	assert.deepEqual([pitched.reseeks, pitched.flamSec < 1], [0, true]);
});

test('control: feed-forward off still holds phase, because the decision follows a smoothed local tempo', () => {
	// Before the smoothed follow lived in phaseLockDecision, freezing the base
	// re-seeked these grids (2 reseeks and tens of seconds of flam). The follow
	// now retunes inside the decision, so the same freeze seeks nothing and
	// flams nothing, and the retune is the sends.
	const ramp = run(RAMP(), grid(126, 0.05, 1500), 120, { fixedBase: true });
	assert.deepEqual([ramp.reseeks, ramp.flamSec, ramp.baseMoves], [0, 0, 0]);
	assert.ok(ramp.sendsPerSec > 0.5, `${ramp.sendsPerSec} sends/s, the follow did not retune`);
	const step = run(grid(128, 0.1, 1500), STEP(), 120, { fixedBase: true });
	assert.deepEqual([step.reseeks, step.flamSec, step.baseMoves], [0, 0, 0]);
	assert.ok(step.sendsPerSec > 0, `${step.sendsPerSec} sends/s`);
});

test('control: constant grids never move the base, exact or ms-rounded (no dither-driven commands)', () => {
	const exact = run(grid(128, 0.1, 1500), grid(126, 0.05, 1500), 120);
	assert.deepEqual([exact.reseeks, exact.flamSec, exact.baseMoves, exact.sendsPerSec], [0, 0, 0, 0]);
	const rounded = run(msRounded(grid(128, 0.1, 1500)), msRounded(grid(126, 0.05, 1500)), 120);
	assert.equal(rounded.reseeks, 0);
	assert.equal(rounded.baseMoves, 0, 'ms dither must stay inside the hysteresis');
	assert.ok(rounded.sendsPerSec < 0.2, `${rounded.sendsPerSec} commands/s`);
});

test('the feed-forward base: unchanged off the grid, inside the hysteresis, and inside the pitch range', () => {
	const M = grid(128, 0.1, 400);
	const F = grid(126, 0.05, 400);
	const input = (over) => ({
		masterBeats: M, masterPositionSec: 60, masterTempo: 1, followerBeats: F, followerPositionSec: 59,
		followerBaseTempo: 128 / 126, normalization: 1, pitchRangePct: 8, ...over
	});
	assert.equal(pl.phaseLockFeedForwardBase(input()), 128 / 126);
	// A base within the hysteresis of what the grids ask is kept exactly.
	const near = (128 / 126) * (1 + pl.PHASE_LOCK_FEED_FORWARD_HYSTERESIS * 0.9);
	assert.equal(pl.phaseLockFeedForwardBase(input({ followerBaseTempo: near })), near);
	// ...and one past it moves to what the grids ask.
	const far = (128 / 126) * (1 + pl.PHASE_LOCK_FEED_FORWARD_HYSTERESIS * 1.1);
	assert.ok(Math.abs(pl.phaseLockFeedForwardBase(input({ followerBaseTempo: far })) - 128 / 126) < 1e-9);
	// Off the grid (or a window running off it) there is nothing to measure.
	assert.equal(pl.phaseLockFeedForwardBase(input({ masterPositionSec: 0.05, followerBaseTempo: 1.2 })), 1.2);
	assert.equal(pl.phaseLockFeedForwardBase(input({ followerPositionSec: F.at(-2).t, followerBaseTempo: 1.2 })), 1.2);
	// A grid asking past the pitch range is held at its edge.
	assert.equal(pl.phaseLockFeedForwardBase(input({ followerBeats: grid(100, 0.05, 400), followerPositionSec: 59, pitchRangePct: 8 })), 1.08);
});

test('SOURCE: both engines take the feed-forward base on every lock tick', () => {
	for (const file of ['src/lib/rb/phase-lock-webaudio.ts', 'src/lib/audio-engine/rust-transport.ts']) {
		const src = readFrontendSource(file);
		assert.match(src, /const forwarded = phaseLockFeedForwardBase\(input\);/, file);
		assert.match(
			src,
			/phaseLockDecision\(\{\s*\.\.\.input,\s*followerBaseTempo: forwarded,\s*trimming: lock\.sent !== lock\.base\s*\}\)/,
			file
		);
	}
});
