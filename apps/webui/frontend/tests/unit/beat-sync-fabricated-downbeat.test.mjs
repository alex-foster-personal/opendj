// Issue #1777: BAR sync must refuse extrapolated fallback downbeat anchors.
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let computeFollowerSyncPlan;
let BAR_SYNC_EXTRAPOLATED_ANCHOR;
let beatIsExtrapolated;
let hasRealBeatGrid;
let effectiveBeatSync;
let gridFeaturesInert;

const BOUNDS = {
	mode: 'bar',
	masterTempoRatio: 1,
	minFollowerTempoRatio: 0.84,
	maxFollowerTempoRatio: 1.16,
	currentContextTimeSec: 0,
	syncAtContextTimeSec: 0.05
};

function regularGrid(bpm, beatCount = 12) {
	const beatInterval = 60 / bpm;
	const beats = [];
	for (let index = 0; index < beatCount; index += 1) {
		beats.push({ n: (index % 4) + 1, bpm, t: index * beatInterval });
	}
	return beats;
}

/** Compact grid: measured 4/4 cadence through 5.656s, then tail from 6.125s. */
function issue1777FollowerGrid() {
	const measured = [
		{ n: 1, bpm: 128, t: 0.5, extrapolated: false },
		{ n: 2, bpm: 128, t: 0.969, extrapolated: false },
		{ n: 3, bpm: 128, t: 1.438, extrapolated: false },
		{ n: 4, bpm: 128, t: 1.906, extrapolated: false },
		{ n: 1, bpm: 128, t: 2.375, extrapolated: false },
		{ n: 2, bpm: 128, t: 2.844, extrapolated: false },
		{ n: 3, bpm: 128, t: 3.312, extrapolated: false },
		{ n: 4, bpm: 128, t: 3.781, extrapolated: false },
		{ n: 1, bpm: 128, t: 4.25, extrapolated: false },
		{ n: 2, bpm: 128, t: 4.719, extrapolated: false },
		{ n: 3, bpm: 128, t: 5.188, extrapolated: false },
		{ n: 4, bpm: 128, t: 5.656, extrapolated: false },
		{ n: 1, bpm: 128, t: 6.125, extrapolated: false }
	];
	const beatInterval = 60 / 128;
	const tail = [];
	for (let index = 0; index < 80; index += 1) {
		tail.push({
			n: ((index + 1) % 4) + 1,
			bpm: 128,
			t: 6.125 + (index + 1) * beatInterval,
			extrapolated: true
		});
	}
	return [...measured, ...tail];
}

before(async () => {
	({
		computeFollowerSyncPlan,
		BAR_SYNC_EXTRAPOLATED_ANCHOR,
		beatIsExtrapolated
	} = await loadTypeScriptModule('src/lib/rb/beat-sync-math.ts'));
	({ hasRealBeatGrid, effectiveBeatSync, gridFeaturesInert } =
		await loadTypeScriptModule('src/lib/player/grid-features.ts'));
});

test('BAR sync in the measured span picks a non-extrapolated anchor', () => {
	const masterGrid = regularGrid(128, 400);
	const followerGrid = issue1777FollowerGrid();
	const plan = computeFollowerSyncPlan({
		...BOUNDS,
		masterGrid,
		followerGrid,
		masterPositionAtSyncSec: 0.2,
		followerPositionSec: 3.0
	});
	assert.equal(beatIsExtrapolated(followerGrid[plan.followerBeatIndex]), false);
	assert.equal(followerGrid[plan.followerBeatIndex].n, 1);
});

test('BAR sync refuses when the follower anchor would be extrapolated', () => {
	const masterGrid = regularGrid(128, 400);
	const followerGrid = issue1777FollowerGrid();
	assert.throws(
		() =>
			computeFollowerSyncPlan({
				...BOUNDS,
				masterGrid,
				followerGrid,
				masterPositionAtSyncSec: 0.2,
				followerPositionSec: 20.0
			}),
		(err) => err instanceof RangeError && err.message === BAR_SYNC_EXTRAPOLATED_ANCHOR
	);
});

test('BAR sync refuses when the master anchor would be extrapolated', () => {
	const masterGrid = issue1777FollowerGrid();
	const followerGrid = issue1777FollowerGrid();
	assert.throws(
		() =>
			computeFollowerSyncPlan({
				...BOUNDS,
				masterGrid,
				followerGrid,
				masterPositionAtSyncSec: 20.0,
				followerPositionSec: 3.0
			}),
		(err) => err instanceof RangeError && err.message === BAR_SYNC_EXTRAPOLATED_ANCHOR
	);
});

test('PQTZ grids without extrapolated still BAR-sync at several positions', () => {
	const masterGrid = regularGrid(128, 200);
	const followerGrid = regularGrid(128, 200);
	for (const masterPositionAtSyncSec of [0.5, 30, 60, 80]) {
		assert.doesNotThrow(() =>
			computeFollowerSyncPlan({
				...BOUNDS,
				masterGrid,
				followerGrid,
				masterPositionAtSyncSec,
				followerPositionSec: 0.02
			})
		);
	}
});

test('tail-heavy fallback grid stays trusted for ticks and the Beat Sync control', () => {
	const followerGrid = issue1777FollowerGrid();
	assert.equal(hasRealBeatGrid(followerGrid), true);
	assert.equal(
		effectiveBeatSync({
			beat_sync_enabled: true,
			anlz: {
				beatgrid: {
					source: 'own',
					status: 'ok',
					beats: followerGrid,
					beat_count: followerGrid.length
				}
			}
		}),
		true
	);
	assert.equal(
		gridFeaturesInert({
			stable_id: 'abc123',
			anlz: {
				beatgrid: {
					source: 'own',
					status: 'ok',
					beats: followerGrid,
					beat_count: followerGrid.length
				}
			}
		}),
		false
	);
});
