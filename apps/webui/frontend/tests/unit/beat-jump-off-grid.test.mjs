import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * SEEK-GRID-02 (#5601): a beat jump whose anchor lies more than one beat
 * outside the beat grid moves N times the grid's EDGE beat interval from the
 * anchor, instead of anchoring on the last real beat (which put a jump from
 * 150 s on a grid ending at 36 s back near 36 s). A jump that lands back
 * within the grid's span snaps to the nearest real beat; inside the grid
 * nothing changes.
 *
 * Regression lines:
 * - if a jump from past a short grid lands near the grid's last beat then broken
 * - if a jump that crosses back into the grid lands off a grid beat then broken
 * - if an in-grid jump stops landing on grid beats then broken
 * - if an off-grid jump past the audio falls back to the last real beat then broken
 */

let math;

before(async () => {
	math = await loadTypeScriptModule('src/lib/rb/beat-sync-math.ts');
});

/** 125 BPM (0.48 s beats) from `first` for `count` beats, bars of four. */
function grid(first, count) {
	return Array.from({ length: count }, (_, i) => ({
		t: Math.round((first + i * 0.48) * 1000) / 1000,
		n: (i % 4) + 1,
		bpm: 125
	}));
}

const SHORT_GRID = grid(0.027, 75); // last beat 35.547 s, edge interval 0.48 s
const LATE_GRID = grid(30, 100); // first beat 30 s
const DURATION_MS = 180_510;
const near = (actual, expected, label) =>
	assert.ok(Math.abs(actual - expected) < 1e-6, `${label}: ${actual} != ${expected}`);

test('[if] a jump starts past the end of a short grid [then] it moves exactly N edge beats, forward and back', () => {
	for (const keepPhase of [false, true]) {
		near(math.beatJumpTargetMs(SHORT_GRID, 150_000, 4, keepPhase), 151_920, `+4 keepPhase=${keepPhase}`);
		near(math.beatJumpTargetMs(SHORT_GRID, 150_000, -4, keepPhase), 148_080, `-4 keepPhase=${keepPhase}`);
		near(math.beatJumpTargetMs(SHORT_GRID, 150_000, 1, keepPhase), 150_480, `+1 keepPhase=${keepPhase}`);
		near(math.beatJumpTargetMs(SHORT_GRID, 150_000, -32, keepPhase), 134_640, `-32 keepPhase=${keepPhase}`);
	}
});

test('[control] the old anchor rule would have landed near the last beat', () => {
	// Nearest beat to 150 s is the last one; this is what the jump used to anchor on.
	assert.equal(math.quantizeToNearestBeat(SHORT_GRID, 150), SHORT_GRID.at(-1).t);
});

test('[if] a backward jump crosses back into the grid [then] it lands on the nearest grid beat', () => {
	// 36.5 s is past the span (35.547 + 0.48); -4 edge beats = 34.58 s, nearest beat 34.587 s.
	near(math.beatJumpTargetMs(SHORT_GRID, 36_500, -4), SHORT_GRID[72].t * 1000, 'back into the grid');
	assert.ok(SHORT_GRID.some((b) => Math.abs(b.t * 1000 - math.beatJumpTargetMs(SHORT_GRID, 36_500, -4)) < 1e-6));
});

test('[if] a jump starts before a late-starting grid [then] it uses the first interval, snaps when it crosses in, stops at 0', () => {
	near(math.beatJumpTargetMs(LATE_GRID, 5_000, 4), 6_920, 'forward before the grid');
	near(math.beatJumpTargetMs(LATE_GRID, 5_000, 52), 30_000, 'crosses in, lands on the first beat');
	assert.equal(math.beatJumpTargetMs(LATE_GRID, 5_000, -16), 0, 'never below 0');
});

test('[overshoot control] an in-grid jump still lands on grid beats, clamped at the grid end', () => {
	near(math.beatJumpTargetMs(SHORT_GRID, 20_100, 4), SHORT_GRID[46].t * 1000, 'in-grid +4 from the nearest beat');
	near(math.beatJumpTargetMs(SHORT_GRID, 20_100, -4), SHORT_GRID[38].t * 1000, 'in-grid -4');
	near(math.beatJumpTargetMs(SHORT_GRID, 20_100, 500), SHORT_GRID.at(-1).t * 1000, 'in-grid jump past the end clamps');
	// Within one beat after the last beat is still on the grid: anchored on the last beat.
	near(math.beatJumpTargetMs(SHORT_GRID, 35_800, -1), SHORT_GRID[73].t * 1000, 'edge tolerance');
});

test('[if] an off-grid jump runs past the audio [then] it steps back whole edge beats, not onto the last real beat', () => {
	near(math.beatJumpTargetWithinDurationMs(SHORT_GRID, 181_000, DURATION_MS), 180_040, 'stepped back');
	const plan = math.beatJumpSeekPlan(SHORT_GRID, 180_000, 4, DURATION_MS, false);
	near(plan.targetMs, 180_480, 'seek plan near the end');
	assert.equal(math.beatJumpMovesTransportWithinDuration(SHORT_GRID, 150_000, 4, DURATION_MS), true);
	assert.equal(math.beatJumpMovesTransportWithinDuration(SHORT_GRID, 180_400, 1, DURATION_MS), false, 'no whole beat left');
});
