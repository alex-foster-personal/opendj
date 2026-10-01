/**
 * ADVERSARIAL (round 1, intentionally RED until fixed): a beat jump on a
 * PLAYING deck is not phase-preserving. It moves the playhead by a
 * non-integer number of beats, so the deck's own rhythm skips and every
 * Beat Sync follower of a jumping master is knocked off phase.
 *
 * Engine path (src/lib/rb/audio-engine.svelte.ts `beatJump`, ~line 3782):
 *   anchorMs = live position projected to the next schedule time
 *   target   = beatJumpTargetMs(grid, anchorMs, n)    -> an exact beat .t
 *   quantizedSeek(deck, target)                        -> fires at that time
 * `beatJumpTargetMs` (src/lib/rb/beat-sync-math.ts:523) first snaps the
 * anchor to its NEAREST beat and drops the fraction, and the seek is not
 * deferred to a beat boundary, so the deck jumps from phase p of its beat to
 * phase 0 of the target beat: a jump of n - p beats. At 128 BPM and p = 0.3
 * that is a 141 ms hiccup in the deck's own groove.
 *
 * With BeatSyncMax off nothing re-anchors the followers of a master that does
 * this (`planSeekSync` returns 'free'), so they are now p beats off. The
 * phase lock then either re-seeks them (p > 0.25: a second audible jump) or
 * trims for tens of seconds (p < 0.25, see
 * beat-sync-adversarial-phase-lock-recovery.test.mjs).
 *
 * The existing deck-loop-beatjump.test.mjs pins the static contract (a
 * between-beats anchor snaps to its nearest beat); that is right for a
 * PAUSED deck and is not what this file disputes.
 *
 * There are TWO phase-erasers on this path, and fixing one is not enough
 * (verified by mutation): `beatJumpTargetMs` drops the fraction, AND
 * `quantizedSeek` (skipGridQuantize=false when no loop is engaged) re-snaps
 * whatever target it is given to the nearest grid line via
 * `quantizedSeekDecisionMs` (src/lib/player/transport/loops.ts:340). The same
 * second eraser hits every quantized seek on a playing deck: waveform click,
 * CUE jump, immediate hot cue.
 *
 * Fix direction: for a playing deck either carry the fractional phase
 * (target + p * interval at the target beat) AND pass skipGridQuantize=true
 * from beatJump, or arm the jump at the deck's next beat boundary, the way
 * BeatSyncMax hot cues already arm at the next downbeat
 * (`planHotCueTrigger`).
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let bsm;
let loops;
let pl;

before(async () => {
	bsm = await loadTypeScriptModule('src/lib/rb/beat-sync-math.ts');
	loops = await loadTypeScriptModule('src/lib/player/transport/loops.ts');
	pl = await loadTypeScriptModule('src/lib/rb/phase-lock.ts');
});

function grid(bpm, startSec, count) {
	const spb = 60 / bpm;
	return Array.from({ length: count }, (_, i) => ({ n: (i % 4) + 1, bpm, t: startSec + i * spb }));
}

const GRID = grid(128, 0.1, 600);
const SPB = 60 / 128;
const DURATION_MS = GRID.at(-1).t * 1000;

/** The engine's composed beat-jump landing for a deck at `anchorMs`. */
function engineBeatJumpLandingMs(anchorMs, beats, quantizeGridBeats) {
	const raw = bsm.beatJumpTargetMs(GRID, anchorMs, beats);
	const clamped = bsm.beatJumpTargetWithinDurationMs(GRID, raw, DURATION_MS);
	// quantizedSeek with quantize on and no loop (the common case).
	return loops.quantizedSeekDecisionMs(GRID, clamped, quantizeGridBeats, false, null).targetMs;
}

for (const [phase, jump] of [[0.1, 4], [0.3, 4], [0.45, 4], [0.3, -4]]) {
	{
		test(`playing deck at phase ${phase}: a ${jump}-beat jump moves exactly ${jump} beats`, () => {
			const anchorMs = (GRID[200].t + phase * SPB) * 1000;
			const landingMs = engineBeatJumpLandingMs(anchorMs, jump, 1);
			const movedBeats =
				pl.gridBeatPosition(GRID, landingMs / 1000) - pl.gridBeatPosition(GRID, anchorMs / 1000);
			// Observed on the unfixed code: movedBeats = jump - phase (e.g. 3.7
			// for +4 at phase 0.3), i.e. a 141 ms rhythm skip at 128 BPM.
			assert.ok(
				Math.abs(movedBeats - jump) < 1e-6,
				`moved ${movedBeats.toFixed(3)} beats: the groove skips ` +
					`${(Math.abs(movedBeats - jump) * SPB * 1000).toFixed(0)} ms`
			);
		});
	}
}

test('control: a jump fired exactly on a beat is already phase-exact', () => {
	const anchorMs = GRID[200].t * 1000;
	const landingMs = engineBeatJumpLandingMs(anchorMs, 4, 1);
	assert.ok(Math.abs(landingMs - GRID[204].t * 1000) < 1e-6);
});
