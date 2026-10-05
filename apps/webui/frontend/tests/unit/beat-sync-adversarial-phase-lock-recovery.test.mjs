/**
 * ADVERSARIAL (round 1; was RED, FIXED by PHASE_LOCK_RESEEK_MS): a phase error
 * just under PHASE_LOCK_RESEEK_BEATS was walked back by the capped trim for tens of
 * seconds, all of it an audible flam.
 *
 * `phaseLockDecision` (src/lib/rb/phase-lock.ts) re-seeks only past a quarter
 * master beat (117 ms at 128 BPM). Below that it trims, capped at
 * PHASE_LOCK_MAX_TRIM (0.3%): 3 ms of correction per wall second. So any
 * step error between ~10 ms (where a flam becomes audible) and 117 ms is
 * corrected at 3 ms/s - a 94 ms (0.2 beat) step needs ~28 s to get under
 * 10 ms. A step that size is what a master gets from any jump that is not
 * phase-preserving: a beat jump or quantized seek on a playing master with
 * BeatSyncMax off (see beat-sync-adversarial-beat-jump-phase.test.mjs), a
 * hot cue fired immediately, a stall/underrun.
 *
 * The module's own docstring already prices this ("a capped trim would take
 * ~40 s") for the quarter-beat case and re-seeks there; the gap is that the
 * same reasoning applies all the way down to the flam threshold.
 *
 * Fix direction (verified by mutation, see findings.md): re-seek when the
 * error exceeds what the capped trim can remove within a few beats, e.g.
 * max(15 ms, PHASE_LOCK_MAX_TRIM * PHASE_LOCK_CORRECTION_BEATS * beat) -
 * or raise the cap while the error is large. The existing jitter and
 * 0.05%-drift simulations in phase-lock.test.mjs must stay green (they are
 * the overshoot control: re-seeking on feed jitter would be worse).
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

const MASTER = grid(128, 0.1, 4000);
const FOLLOWER = grid(126, 0.05, 4000);
const BASE = 128 / 126;
const AUDIBLE_FLAM_MS = 10;
const TICK_SEC = 1 / 30;

/**
 * Closed loop at 30 Hz with an exact base rate (no drift at all): only the
 * initial step error is under test. A re-seek is modeled as an exact re-join.
 */
function simulateStep(stepBeats) {
	const m0 = 60;
	const mb = pl.gridBeatPosition(MASTER, m0);
	const i = Math.floor(mb);
	let m = m0;
	let f = FOLLOWER[i].t + (mb - i + stepBeats) * (FOLLOWER[i + 1].t - FOLLOWER[i].t);
	let sent = BASE;
	let flamSec = 0;
	let reseeks = 0;
	// The lock is long past its join when the step lands (the minimum interval
	// after a join has its own tests in phase-lock-jitter.test.mjs).
	let overLineTicks = 0;
	for (let k = 0; k < 30 * 120; k++) {
		const d = pl.phaseLockDecision({
			masterBeats: MASTER,
			masterPositionSec: m,
			masterTempo: 1,
			followerBeats: FOLLOWER,
			followerPositionSec: f,
			followerBaseTempo: BASE,
			pitchRangePct: 8,
			trimming: sent !== BASE,
			sinceJoinSec: 600,
			overLineTicks
		});
		overLineTicks = d.overLineTicks;
		if (d.action === 'reseek') {
			reseeks += 1;
			const b = pl.gridBeatPosition(MASTER, m);
			const j = Math.floor(b);
			f = FOLLOWER[j].t + (b - j) * (FOLLOWER[j + 1].t - FOLLOWER[j].t);
			sent = BASE;
			overLineTicks = 0;
			continue;
		}
		if (Math.abs(d.errorMs) >= AUDIBLE_FLAM_MS) flamSec += TICK_SEC;
		if (pl.phaseLockShouldSend(sent, d.tempo, BASE)) sent = d.tempo;
		m += TICK_SEC;
		f += TICK_SEC * sent;
	}
	return { flamSec, reseeks };
}

for (const stepBeats of [0.05, 0.1, 0.2, 0.24]) {
	test(`a ${stepBeats}-beat step (${(stepBeats * 468.75).toFixed(0)} ms) is audible for at most 2 s`, () => {
		const { flamSec, reseeks } = simulateStep(stepBeats);
		// Observed on the unfixed code (128 BPM): 0.05 -> ~4.5 s, 0.1 -> ~12 s,
		// 0.2 -> ~28 s, 0.24 -> ~34 s of flam, 0 re-seeks.
		assert.ok(
			flamSec <= 2,
			`follower flammed >= ${AUDIBLE_FLAM_MS} ms for ${flamSec.toFixed(1)} s ` +
				`(re-seeks: ${reseeks}) after a ${stepBeats}-beat step`
		);
	});
}

test('control: a 0.3-beat step is re-seeked as soon as it is confirmed (the existing quarter-beat rule)', () => {
	const { flamSec, reseeks } = simulateStep(0.3);
	assert.equal(reseeks, 1);
	assert.ok(flamSec < 0.1, `${flamSec}`);
});
