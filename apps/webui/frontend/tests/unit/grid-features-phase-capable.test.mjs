// issue #2607: phase-capable grid predicate for Beat Sync and beat loops
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const REAL_BEATS = [
	{ n: 1, bpm: 127, t: 0.135 },
	{ n: 2, bpm: 127, t: 0.608 },
	{ n: 3, bpm: 127, t: 1.08 },
	{ n: 4, bpm: 127, t: 1.552 }
];

/** Structurally valid cadence with no bar-1 downbeat (issue #2607 repro class). */
const NO_DOWNBEAT_BEATS = [
	{ n: 2, bpm: 127, t: 0.608 },
	{ n: 3, bpm: 127, t: 1.08 },
	{ n: 4, bpm: 127, t: 1.552 }
];

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

function ownAnlz(beats) {
	return {
		beatgrid: {
			source: 'own',
			status: 'ok',
			beats,
			beat_count: beats.length
		}
	};
}

let grid;
let guards;

before(async () => {
	grid = await loadTypeScriptModule('src/lib/player/grid-features.ts');
	guards = await loadTypeScriptModule('src/lib/player/beatgrid-resync-guards.ts');
});

test('a structurally valid grid without n===1 stays trusted for quantize but not phase work', () => {
	const anlz = ownAnlz(NO_DOWNBEAT_BEATS);
	const st = { stable_id: 'x', quantize_enabled: true, beat_sync_enabled: true, anlz };
	assert.equal(grid.hasTrustedBeatGrid(anlz), true);
	assert.equal(grid.hasPhaseCapableBeatGrid(anlz), false);
	assert.equal(grid.effectiveQuantize(st), true);
	assert.equal(grid.effectiveBeatSync(st), false);
	assert.equal(grid.gridFeaturesInert(st), false);
	assert.equal(grid.beatSyncFeaturesInert(st), true);
	assert.match(grid.beatSyncInertTip(st), /PQTZ downbeat/i);
});

test('a normal PQTZ grid and the issue #1777 tail-heavy fallback grid stay phase-capable', () => {
	for (const beats of [REAL_BEATS, issue1777FollowerGrid()]) {
		const anlz = ownAnlz(beats);
		const st = { stable_id: 'x', quantize_enabled: true, beat_sync_enabled: true, anlz };
		assert.equal(grid.hasPhaseCapableBeatGrid(anlz), true);
		assert.equal(grid.beatSyncFeaturesInert(st), false);
		assert.equal(grid.effectiveBeatSync(st), true);
	}
});

test('a trusted [2,3,4] master is not phase-lockable and mixes without Beat Sync when followed', () => {
	const masterAnlz = ownAnlz(NO_DOWNBEAT_BEATS);
	const master = { anlz: masterAnlz };
	const follower = {
		stable_id: 'follower',
		beat_sync_enabled: true,
		anlz: ownAnlz(REAL_BEATS)
	};
	assert.equal(grid.hasTrustedBeatGrid(masterAnlz), true);
	assert.equal(grid.deckHasPhaseCapableBeatGrid(master), false);
	assert.equal(grid.canPhaseLockTo(follower, master), false);
	assert.equal(grid.handoverMixesWithoutBeatSync(master, 1), true);
	assert.equal(grid.beatSyncClockMaster(1, master), null);
});

test('requirePhaseCapableBeatGrid names the missing downbeat instead of throwing RangeError', () => {
	const st = {
		deck_id: 2,
		stable_id: 'x',
		anlz: ownAnlz(NO_DOWNBEAT_BEATS)
	};
	assert.throws(
		() => guards.requirePhaseCapableBeatGrid(st, 'engageBeatLoop'),
		(err) => err instanceof Error && /requires a PQTZ downbeat/i.test(err.message)
	);
	assert.throws(
		() => guards.requirePhaseCapableBeatGrid(st, 'engageBeatLoop'),
		(err) => !/no PQTZ downbeat \(n===1\) at or before/i.test(err.message)
	);
});
