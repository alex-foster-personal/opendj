/**
 * ADVERSARIAL (round 4; was RED, FIXED by tempoLockToleranceBpm): an ordinary phase-lock
 * trim makes a correctly synced follower read OFF TEMPO.
 *
 * The phase lock (NAE-19) corrects phase by trimming the follower's tempo up
 * to PHASE_LOCK_MAX_TRIM (0.3%) around its sync base, and the trim is a real
 * tempo write: Web Audio sets `st.pitch = scheduledTempoRatio` on every
 * schedule (audio-engine.svelte.ts ~line 1551), Rust mirrors the engine's
 * tempo into `st.pitch` every frame (rust-engine.ts `mirrorEngineState`). The
 * jog dial's live BPM is `playbackBpm(..., tempoRatio: deck.pitch)` and its
 * off-tempo tint is `isTempoLockedToMaster(liveBpm, masterBpm)` with
 * DEFAULT_TEMPO_LOCK_TOLERANCE_BPM = 0.1 (beat-sync-math.ts:660,
 * JogDial.svelte ~line 84). 0.1 BPM is 0.078% at 128 BPM, so any trim past
 * that - every trim for an error over ~1.5 ms, which saturates to 0.3% at
 * ~5.6 ms - lights "Off tempo: 128.4 BPM vs master 128.0" on a follower that
 * is locked and in phase. The BPM readout wobbles by up to 0.38 BPM with it.
 *
 * DJ-visible: the deck UI says sync broke exactly while sync is working. The
 * e2e polls that use the same 0.1 tolerance (`_tempoLockedToMaster` in
 * performance-controls.spec.ts) can flake on the same trims.
 *
 * Fix direction: judge (and display) the follower against its lock BASE, not
 * the trimmed tempo - or widen the lock tolerance to at least
 * PHASE_LOCK_MAX_TRIM of the master BPM. The control below keeps a real
 * 1 BPM mismatch flagged (overshoot guard).
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

const MASTER = grid(128, 0.1, 1200);
const FOLLOWER = grid(126, 0.05, 1200);

function liveBpm(beats, positionSec, tempoRatio) {
	return bsm.playbackBpm({ beats, positionSec, tempoRatio, tagBpm: null });
}

test('a follower 6 ms off phase, under the trim the lock itself chose, still reads tempo-locked', () => {
	const join = bsm.computeFollowerSyncPlan({
		masterGrid: MASTER,
		followerGrid: FOLLOWER,
		masterPositionAtSyncSec: 60,
		masterTempoRatio: 1,
		followerPositionSec: 59,
		currentContextTimeSec: 0,
		syncAtContextTimeSec: 0.1,
		minFollowerTempoRatio: 0.92,
		maxFollowerTempoRatio: 1.08,
		mode: 'beat'
	});
	const decision = pl.phaseLockDecision({
		masterBeats: MASTER,
		masterPositionSec: 60,
		masterTempo: 1,
		followerBeats: FOLLOWER,
		followerPositionSec: join.followerPositionSec - 0.006 * join.followerTempoRatio,
		followerBaseTempo: join.followerTempoRatio,
		pitchRangePct: 8,
		sinceJoinSec: 600,
		overLineTicks: 0
	});
	assert.equal(decision.action, 'trim', 'precondition: a normal trim, not a re-seek');
	const master = liveBpm(MASTER, 60, 1);
	const base = liveBpm(FOLLOWER, join.followerPositionSec, join.followerTempoRatio);
	const trimmed = liveBpm(FOLLOWER, join.followerPositionSec, decision.tempo);
	assert.ok(bsm.isTempoLockedToMaster(base, master), 'precondition: the base is locked');
	// Observed on the unfixed code: trimmed 128.38 vs master 128.00 -> false.
	assert.ok(
		bsm.isTempoLockedToMaster(trimmed, master),
		`trimmed follower reads ${trimmed.toFixed(2)} BPM vs master ${master.toFixed(2)}: "Off tempo"`
	);
});

test('control: a real 1 BPM mismatch is still reported off tempo', () => {
	assert.equal(bsm.isTempoLockedToMaster(129, 128), false);
	assert.equal(bsm.isTempoLockedToMaster(127, 128), false);
	assert.equal(bsm.isTempoLockedToMaster(64.5, 128), false);
	assert.equal(bsm.isTempoLockedToMaster(257, 128), false);
	// ...and just past the trim cap plus the display slack, on every fold.
	for (const fold of [1, 0.5, 2]) {
		const folded = 128 * fold;
		const edge = 0.1 + pl.PHASE_LOCK_MAX_TRIM * folded;
		assert.equal(bsm.isTempoLockedToMaster(folded + edge * 0.99, 128), true, `fold ${fold} inside`);
		assert.equal(bsm.isTempoLockedToMaster(folded + edge * 1.01, 128), false, `fold ${fold} outside`);
	}
});

test('control: an explicit tolerance is still honored exactly', () => {
	assert.equal(bsm.isTempoLockedToMaster(128.2, 128, 0.1), false);
	assert.equal(bsm.isTempoLockedToMaster(128.05, 128, 0.1), true);
});
