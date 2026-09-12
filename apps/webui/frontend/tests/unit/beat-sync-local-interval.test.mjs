import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

function regularGrid(bpm, beatCount = 12) {
	const beatIntervalSec = 60 / bpm;
	return Array.from({ length: beatCount }, (_, index) => ({
		n: (index % 4) + 1,
		bpm,
		t: index * beatIntervalSec
	}));
}

function twoTempoGrid(firstBpm, firstBeats, secondBpm, secondBeats) {
	const beats = [];
	let t = 0;
	for (let index = 0; index < firstBeats + secondBeats; index++) {
		beats.push({ n: (index % 4) + 1, bpm: index < firstBeats ? firstBpm : secondBpm, t });
		t += 60 / (index < firstBeats ? firstBpm : secondBpm);
	}
	return beats;
}

let computeFollowerSyncPlan;
let gridBpmAt;
let validateBeatGrid;
let exactBeatLoopRangeMs;

const FIRST_BEATS = 80;
const SECOND_BEATS = 80;
const EARLY_PROBE = 20;
const LATE_PROBE = FIRST_BEATS + 40;
const TRACK_MEAN_BPM = 130;
const TRACK_MEAN_RATIO = 120 / TRACK_MEAN_BPM;

before(async () => {
	({ computeFollowerSyncPlan, gridBpmAt, validateBeatGrid } = await loadTypeScriptModule(
		'src/lib/rb/beat-sync-math.ts'
	));
	({ exactBeatLoopRangeMs } = await loadTypeScriptModule('src/lib/player/transport/loops.ts'));
});

test('validateBeatGrid accepts varying per-beat bpm with strictly increasing times', () => {
	assert.doesNotThrow(() => validateBeatGrid(twoTempoGrid(120, FIRST_BEATS, 140, SECOND_BEATS)));
});

test('gridBpmAt at play position uses local interval bpm not track mean', () => {
	const grid = twoTempoGrid(120, FIRST_BEATS, 140, SECOND_BEATS);
	const earlyBpm = gridBpmAt(grid, grid[EARLY_PROBE].t + 0.01);
	const lateBpm = gridBpmAt(grid, grid[LATE_PROBE].t + 0.01);
	assert.ok(Math.abs(earlyBpm - 120) < 1e-3, `early bpm ${earlyBpm} expected ~120`);
	assert.ok(Math.abs(lateBpm - 140) < 1e-3, `late bpm ${lateBpm} expected ~140`);
	assert.ok(Math.abs(earlyBpm - TRACK_MEAN_BPM) > 5);
	assert.ok(Math.abs(lateBpm - TRACK_MEAN_BPM) > 5);
});

test('computeFollowerSyncPlan ratio follows local interval bpm not track mean', () => {
	const masterGrid = regularGrid(120, 400);
	const followerGrid = twoTempoGrid(120, FIRST_BEATS, 140, SECOND_BEATS);
	const shared = {
		masterGrid,
		followerGrid,
		masterPositionAtSyncSec: masterGrid[10].t + 0.01,
		masterTempoRatio: 1,
		currentContextTimeSec: 0,
		syncAtContextTimeSec: 0.05,
		minFollowerTempoRatio: 0.84,
		maxFollowerTempoRatio: 1.16,
		mode: 'beat'
	};

	const earlyPlan = computeFollowerSyncPlan({
		...shared,
		followerPositionSec: followerGrid[EARLY_PROBE].t + 0.01
	});
	assert.ok(Math.abs(earlyPlan.followerTempoRatio - 1) < 1e-3);
	assert.ok(Math.abs(earlyPlan.followerTempoRatio - TRACK_MEAN_RATIO) > 0.05);

	const latePlan = computeFollowerSyncPlan({
		...shared,
		followerPositionSec: followerGrid[LATE_PROBE].t + 0.01
	});
	const lateExpectedRatio = 120 / 140;
	assert.ok(Math.abs(latePlan.followerTempoRatio - lateExpectedRatio) < 1e-3);
	assert.ok(Math.abs(latePlan.followerTempoRatio - TRACK_MEAN_RATIO) > 0.05);
});

test('exactBeatLoopRangeMs accepts multi-anchor grid in both tempo regions', () => {
	const grid = twoTempoGrid(120, FIRST_BEATS, 140, SECOND_BEATS);
	const earlyRange = exactBeatLoopRangeMs(grid, grid[EARLY_PROBE].t * 1000, 4);
	const lateRange = exactBeatLoopRangeMs(grid, grid[LATE_PROBE].t * 1000, 4);
	assert.ok(Number.isFinite(earlyRange.in_ms));
	assert.ok(Number.isFinite(earlyRange.out_ms));
	assert.ok(earlyRange.in_ms < earlyRange.out_ms);
	assert.ok(Number.isFinite(lateRange.in_ms));
	assert.ok(Number.isFinite(lateRange.out_ms));
	assert.ok(lateRange.in_ms < lateRange.out_ms);
});
