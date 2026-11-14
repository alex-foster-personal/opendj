/**
 * Issue #10781 (S4): the edge sampler launch plan (spec section 4.3, 4.4), ported from the v1 reference.
 * A launch lands on the master's next quantise point, a matched rate follows the master's tempo
 * folded into [0.84, 1.16], key lock keeps pitch through the stretcher and vinyl lets it follow rate.
 *
 * Regression lines:
 * - if quant is bar then the start is never in the past and lands on a downbeat
 * - if the master changes tempo then the re-read rate follows it
 * - if a slot is key locked then pitch is kept, and if it is vinyl then pitch follows rate
 * - if repeat is 4 beats on an 8-beat region then the loop window is 4 beats
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let L;
before(async () => {
	L = await loadTypeScriptModule('src/lib/player/sampler/sampler-launch.ts');
});

// 120 BPM grid: beat every 0.5 s, 64 beats, bar starts at t = 0.
const grid = Array.from({ length: 64 }, (_, i) => ({ n: (i % 4) + 1, t: i * 0.5 }));
const byTime = (ctxSec, now, pos, ratio = 1) => pos + (ctxSec - now) * ratio;
const beatAt = (t) => grid.find((b) => Math.abs(b.t - t) < 1e-9);

//----- launch quantise

test('if quant is bar then the start is never in the past and lands on a downbeat', () => {
	/** [if] quant is bar [then] the start is a downbeat at least LAUNCH_LEAD_SEC away, [else stop]. */
	const at = L.nextLaunchCtxSec(grid, 3.1, 100, 1, 'bar', 0);
	assert.ok(at >= 100 + L.LAUNCH_LEAD_SEC && beatAt(byTime(at, 100, 3.1))?.n === 1, String(at));
});

test('if the playhead sits 10 ms before a downbeat then bar skips to the next one', () => {
	/** [if] the next downbeat is closer than the safe lead [then] the launch takes the one after, [else stop]. */
	assert.equal(byTime(L.nextLaunchCtxSec(grid, 1.99, 0, 1, 'bar', 0), 0, 1.99), 4);
});

test('if quant is beat then the start lands on the next beat', () => {
	/** [if] quant is beat [then] the start is the next grid beat, [else stop]. */
	assert.equal(byTime(L.nextLaunchCtxSec(grid, 3.1, 0, 1, 'beat', 0), 0, 3.1), 3.5);
});

test('if quant is 4bars then the start lands on a 4-bar boundary', () => {
	/** [if] quant is 4bars [then] the start is every 4th downbeat from the grid's first, [else stop]. */
	assert.equal(byTime(L.nextLaunchCtxSec(grid, 3.1, 0, 1, '4bars', 0), 0, 3.1), 8);
});

test('if the downbeat offset is +1 then the start moves one whole beat later', () => {
	/** [if] the slot's downbeat offset is +1 [then] the start is one beat after the downbeat, [else stop]. */
	assert.equal(byTime(L.nextLaunchCtxSec(grid, 3.1, 0, 1, 'bar', 1), 0, 3.1), 4.5);
});

test('if the master runs at 1.25x then the ctx delay shrinks by that ratio', () => {
	/** [if] the master plays at 1.25x [then] the context delay to the downbeat is divided by 1.25, [else stop]. */
	assert.ok(Math.abs(L.nextLaunchCtxSec(grid, 3.5, 10, 1.25, 'bar', 0) - (10 + 0.5 / 1.25)) < 1e-9);
});

test('if quant is off then the start is now plus the safe lead', () => {
	/** [if] quant is off [then] the start is now plus LAUNCH_LEAD_SEC, [else stop]. */
	assert.equal(L.nextLaunchCtxSec(grid, 3.1, 7, 1, 'off', 0), 7 + L.LAUNCH_LEAD_SEC);
});

test('if the grid has no quantise point left then it throws', () => {
	/** [if] the playhead is past the last downbeat [then] the launch throws instead of starting late, [else stop]. */
	assert.throws(() => L.nextLaunchCtxSec(grid, 40, 0, 1, 'bar', 0), /no quantise point/);
});

test('if the downbeat offset is fractional or the master ratio is 0 then it throws', () => {
	/** [if] the offset is not whole beats or the master tempo ratio is 0 [then] the launch throws, [else stop]. */
	assert.throws(() => L.nextLaunchCtxSec(grid, 3.1, 0, 1, 'bar', 0.5), /whole beats/);
	assert.throws(() => L.nextLaunchCtxSec(grid, 3.1, 0, 0, 'bar', 0), /tempoRatio/);
});

//----- tempo

test('if tempo is match then 70 BPM folds to ~140 against 124', () => {
	/** [if] a 70 BPM slot matches a 124 BPM master [then] the rate is 124/140 and in the fold range, [else stop]. */
	const rate = L.playbackRate(70, 124, 'match');
	assert.ok(Math.abs(rate - 124 / 140) < 1e-9);
	assert.equal(L.rateInFoldRange(rate), true);
});

test('if the master changes tempo then the re-read rate follows it', () => {
	/** [if] the master moves 124 to 126 BPM [then] the 10 Hz re-read rate for a 124 slot becomes 126/124, [else stop]. */
	assert.equal(L.playbackRate(124, 124, 'match'), 1);
	assert.ok(Math.abs(L.playbackRate(124, 126, 'match') - 126 / 124) < 1e-9);
});

test('if tempo is own then the rate is 1', () => {
	/** [if] the slot plays at its own tempo [then] the rate is 1 whatever the master, [else stop]. */
	assert.equal(L.playbackRate(70, 124, 'own'), 1);
});

test('if no master plays then the rate is 1 (FREE)', () => {
	/** [if] no playing master exists [then] the voice runs free at native tempo, [else stop]. */
	assert.equal(L.playbackRate(70, null, 'match'), 1);
});

test('if a BPM is 0 under match then it throws', () => {
	/** [if] the slot's native BPM is 0 [then] the matched rate throws instead of returning Infinity, [else stop]. */
	assert.throws(() => L.playbackRate(0, 124, 'match'), /BPMs must be > 0/);
});

//----- key

test('if key is auto and keys match then the shift is 0 under key lock', () => {
	/** [if] slot and master are both 8A [then] the unshifted match wins and pitch is kept, [else stop]. */
	assert.deepEqual(L.keyPlan('8A', '8A', 'auto', 1.05), { semitones: 0, masterTempo: true });
});

test('if key is auto and keys clash then a nonzero shift of at most 3 is picked with master tempo', () => {
	/** [if] 3A plays against 8A [then] a shift of 1 to 3 semitones is planned through the stretcher, [else stop]. */
	const p = L.keyPlan('3A', '8A', 'auto', 1);
	assert.ok(p.masterTempo && p.semitones !== 0 && Math.abs(p.semitones) <= 3, JSON.stringify(p));
});

test('if key is master_tempo then there is no shift and pitch is preserved', () => {
	/** [if] the slot is key locked at rate 1.1 [then] there is no shift and pitch is kept, [else stop]. */
	assert.deepEqual(L.keyPlan('3A', '8A', 'master_tempo', 1.1), { semitones: 0, masterTempo: true });
});

test('if key is off (vinyl) then pitch follows rate', () => {
	/** [if] the slot is vinyl at rate 2 [then] pitch rises 12 semitones with no stretcher, [else stop]. */
	assert.deepEqual(L.keyPlan('3A', '8A', 'off', 2), { semitones: 12, masterTempo: false });
});

//----- loop window and playhead

test('if repeat is 4 beats on an 8-beat region then the loop window is 4 beats', () => {
	/** [if] repeat is 4 on an 8-beat, 4 s region [then] the loop window ends at 2 s and loops, [else stop]. */
	assert.deepEqual(L.loopWindow(8, 4, 4), { loopStartSec: 0, loopEndSec: 2, loop: true });
});

test('if repeat is once then the window is the whole region and does not loop', () => {
	/** [if] repeat is once [then] the window is the whole region with loop false, [else stop]. */
	assert.deepEqual(L.loopWindow(8, 'once', 4), { loopStartSec: 0, loopEndSec: 4, loop: false });
});

test('if the playhead is 1.25 passes in then the fraction wraps to 0.25', () => {
	/** [if] 2.5 s have passed on a 2 s pass [then] the playhead fraction is 0.25, [else stop]. */
	assert.ok(Math.abs(L.playheadFraction(12.5, 10, 2) - 0.25) < 1e-9);
});

test('if the voice has not started yet then the fraction is 0', () => {
	/** [if] now is before the scheduled start [then] the playhead fraction is 0, [else stop]. */
	assert.equal(L.playheadFraction(9, 10, 2), 0);
});
