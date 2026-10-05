/**
 * ADVERSARIAL (round 2): a quantized seek or beat jump on a PLAYING master
 * must keep the master's own beat phase at the instant it LANDS.
 *
 * Measured in the real engine (performance-beat-sync-adversarial.spec.ts) with
 * the landing fixed in track seconds, as before this fix: a +4 beat jump on a
 * playing 128 BPM master skipped 165.2 ms of its own rhythm with BeatSyncMax
 * on (8.7 ms off), and a quantized click skipped 169.6 ms on / 117.5 ms off.
 * The schedule lands later than the instant the target was computed for
 * (`_scheduleDeckSerial` moves `when`, BeatSyncMax adds latency plus a
 * safety margin before `syncAt`), so a fixed second lands that much behind.
 *
 * These tests pin the math in src/lib/rb/beat-sync-math.ts (`phaseKeepingLandingSec`) in both
 * directions, and pin that the engine evaluates it at the landing instant.
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

let phaseKeepingLandingSec;

before(async () => {
	({ phaseKeepingLandingSec } = await loadTypeScriptModule('src/lib/rb/beat-sync-math.ts'));
});

/** A constant-tempo grid: `count` beats, `bpm`, first beat at `offset`. */
function grid(count, bpm, offset = 0) {
	const beat = 60 / bpm;
	return Array.from({ length: count }, (_, i) => ({ n: (i % 4) + 1, bpm, t: offset + i * beat }));
}

const B = grid(200, 120); // 0.5 s per beat, so beat index i sits at i / 2 s
const END = 1e9; // no decoded-duration bound unless a test sets one
const close = (actual, expected, label) =>
	assert.ok(Math.abs(actual - expected) < 1e-9, `${label}: ${actual} != ${expected}`);
const click = (current, target, end = END) => phaseKeepingLandingSec(B, current * 0.5, null, target * 0.5, end);

test('a quantized click lands on the clicked beat at the phase the deck plays when it lands', () => {
	// The DJ clicks beat 20 (already snapped) while the deck plays beat 5.3;
	// by the time the schedule lands the playhead is at 5.6. Land at 20.6, NOT
	// at the fixed beat 20 (the pre-fix behavior: a skip of 0.6 beat = 300 ms).
	close(click(5.6, 20), 20.6 * 0.5, 'click landing');
	// Backward clicks keep the phase too.
	close(click(40.25, 8), 8.25 * 0.5, 'backward click');
});

test('a click lands on the CLICKED beat even when the playhead crossed a beat (overshoot control)', () => {
	// A whole-beat delta fixed at decision time (5.9 -> 20 is +15) would land
	// on 21.1 once the playhead reached 6.1: one beat late.
	close(click(6.1, 20), 20.1 * 0.5, 'crossed a beat');
	// The target rounds to its nearest beat.
	close(click(6.1, 19.6), 20.1 * 0.5, 'nearest beat');
	close(click(6.1, 19.4), 19.1 * 0.5, 'nearest beat below');
});

test('a beat jump moves EXACTLY the beats asked from wherever the playhead is when it lands', () => {
	// beatJump hands its `beats` through; +4 from 6.0 lands on 10.0, the
	// playhead's own phase, not the 9.7 decided from an earlier 5.7.
	close(phaseKeepingLandingSec(B, 6.0 * 0.5, 4, 9.7 * 0.5, END), 10 * 0.5, 'jump landing');
	close(phaseKeepingLandingSec(B, 6.25 * 0.5, -4, 0, END), 2.25 * 0.5, 'backward jump keeps phase');
	// A jump ignores targetSec unless it has to fall back (overshoot control).
	close(phaseKeepingLandingSec(B, 6.25 * 0.5, 4, 50, END), 10.25 * 0.5, 'jump ignores target');
});

test('the landing follows a variable-tempo grid, not a constant beat length', () => {
	// 8 beats at 0.5 s, then 8 beats at 0.4 s.
	const varying = [...grid(8, 120)];
	for (let i = 0; i < 8; i++) varying.push({ n: ((8 + i) % 4) + 1, bpm: 150, t: 3.5 + (i + 1) * 0.4 });
	const landing = phaseKeepingLandingSec(varying, 1.25, 8, 0, END); // beat 2.5 + 8 = beat 10.5
	close(landing, 3.5 + 3.5 * 0.4, 'variable-tempo landing'); // beat 7 at 3.5 s, then 3.5 short beats
});

test('off the grid there is no phase to keep: the fixed target lands', () => {
	const late = grid(40, 120, 2); // first beat at 2 s, last at 21.5 s
	assert.equal(phaseKeepingLandingSec(late, 1.0, null, 10, END), 10, 'playhead before the first beat');
	assert.equal(phaseKeepingLandingSec(late, 5.0, null, 30, END), 30, 'target past the last beat');
	assert.equal(phaseKeepingLandingSec(late, 1.0, 4, 7.25, END), 7.25, 'jump from before the grid');
	assert.equal(phaseKeepingLandingSec(late, 20.0, 4, 7.25, END), 7.25, 'jump past the last beat');
	assert.equal(phaseKeepingLandingSec(late, 3.0, -4, 7.25, END), 7.25, 'jump before the first beat');
	// Control: the same call inside the grid does NOT fall back.
	close(phaseKeepingLandingSec(late, 3.0, 4, 7.25, END), 5.0, 'in-grid landing');
});

test('a landing past the decoded audio falls back, one exactly at the end does not', () => {
	// A grid can run past the decoded buffer (beatJumpTargetWithinDurationMs).
	assert.equal(phaseKeepingLandingSec(B, 10.0, 4, 9.5, 11.5), 9.5, 'past the end');
	close(phaseKeepingLandingSec(B, 10.0, 4, 9.5, 12.0), 12.0, 'exactly at the end');
});

test('no grid (a paused, unquantized or gridless deck): no phase to keep, the fixed target lands', () => {
	assert.equal(phaseKeepingLandingSec(null, 2.0, null, 7.25, END), 7.25);
	assert.equal(phaseKeepingLandingSec(null, 2.0, 4, 7.25, END), 7.25);
});

test('source pin: quantizedSeek lands at the effective instant on both schedule paths', () => {
	const source = readFileSync(
		fileURLToPath(new URL('../../src/lib/rb/audio-engine.svelte.ts', import.meta.url)),
		'utf8'
	);
	// The grid is read through the never-throwing paths, for a playing deck,
	// and for a click only when quantize applies.
	assert.match(
		source,
		/const grid = !rt\.desiredActive \? null : jumpBeats != null \? \(st\.anlz\?\.beatgrid\.beats \?\? null\) : skipGridQuantize \? null : seekBeats;/
	);
	// The landing is a function of the instant it lands, re-projected there.
	assert.match(
		source,
		/\(at: number\): number => phaseKeepingLandingSec\(grid, _projectPositionAt\(deck, at\), jumpBeats \?\? null, targetMs \/ 1000, durMs \/ 1000\)/
	);
	// BeatSyncMax master path and free path both take that function.
	assert.match(source, /masterSchedule: \{[^}]*positionSec: landing\s*\}/);
	assert.match(source, /_scheduleDeck\(\s*deck,\s*when,\s*landing,/);
	// _synchronizeFollowers evaluates it at syncAt, not at decision time.
	assert.match(source, /options\.masterSchedule\?\.positionSec\?\.\(syncAt\) \?\? projectedMasterSec/);
	// A beat jump hands its beat count through on both its paths (loop and plain).
	const start = source.indexOf('async beatJump(deck: DeckId, beats: number): Promise<void> {');
	const body = source.slice(start, source.indexOf('\n\t}\n', start));
	assert.equal((body.match(/, undefined, beats\);/g) ?? []).length, 2);
});
