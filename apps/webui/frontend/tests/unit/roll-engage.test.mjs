// requirement: FXROLL-02
// [if] a 1-beat-or-longer roll with quantize on engages anywhere but the next PQTZ beat after now plus lead [then] broken
// [if] a sub-beat roll does not start on the nearer slot from the previous beat [then] broken
// [if] quantize off stops returning the phase-folded plan [then] broken
// [if] a grid with fewer than 2 beats engages instead of throwing [then] broken
// [if] the planned onset lands more than 1.0 ms off the grid slot [then] broken
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const GRID = Array.from({ length: 16 }, (_, i) => ({ n: (i % 4) + 1, bpm: 120, t: i * 0.5 }));
const UNEVEN = [
	{ n: 1, bpm: 120, t: 0 },
	{ n: 2, bpm: 120, t: 0.5 },
	{ n: 3, bpm: 100, t: 1.1 },
	{ n: 4, bpm: 100, t: 1.7 }
];
const near = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`);

let re;
let loops;
before(async () => {
	re = await loadTypeScriptModule('src/lib/player/transport/roll-engage.ts');
	loops = await loadTypeScriptModule('src/lib/player/transport/loops.ts');
});

const input = (over) => ({
	quantize: true,
	beats: GRID,
	rollBeats: 1,
	earliestWhenContextSec: 10,
	positionAtEarliestSec: 1.3,
	tempoRatio: 1,
	currentPositionMs: 1300,
	engagedLoop: null,
	...over
});

test('FXROLL-02 roll of 1 beat or more engages on the next PQTZ beat with playhead at loop start', () => {
	const plan = re.planRollEngage(input({}));
	assert.equal(plan.kind, 'quantized');
	near(plan.engageAtContextSec, 10.2);
	near(plan.playheadSec, 1.5);
	near(plan.loop.in_ms, 1500);
	const two = re.planRollEngage(input({ rollBeats: 2 }));
	near(two.playheadSec, 1.5);
	near(two.loop.out_ms, 2500);
	near(re.planRollEngage(input({ tempoRatio: 1.25 })).engageAtContextSec, 10 + 0.2 / 1.25);
	near(re.planRollEngage(input({ positionAtEarliestSec: 1.5 })).engageAtContextSec, 10);
});

test('FXROLL-02 sub-beat roll starts on the nearer slot from the previous beat', () => {
	assert.deepEqual(loops.rollLoopRangeMs(GRID, 1130, 0.25), { in_ms: 1125, out_ms: 1250 });
	assert.deepEqual(loops.rollLoopRangeMs(GRID, 1190, 0.25), { in_ms: 1250, out_ms: 1375 });
	assert.equal(loops.rollLoopRangeMs(GRID, 1062.5, 0.25).in_ms, 1000); // tie goes earlier
	assert.equal(loops.rollLoopRangeMs(GRID, 1490, 0.25).in_ms, 1500);
	const plan = re.planRollEngage(input({ positionAtEarliestSec: 1.13, rollBeats: 0.25 }));
	near(plan.engageAtContextSec, 10.12);
	near(plan.loop.in_ms, 1250);
	near(plan.loop.out_ms, 1375);
	const uneven = loops.rollLoopRangeMs(UNEVEN, 700, 0.5);
	near(uneven.in_ms, 800);
	near(uneven.out_ms, 1100);
});

test('FXROLL-02 quantize off keeps the phase-folded engage', () => {
	const plan = re.planRollEngage(input({ quantize: false, currentPositionMs: 1300 }));
	assert.equal(plan.kind, 'phase_folded');
	assert.equal(plan.engageAtContextSec, undefined);
	assert.deepEqual(plan.loop, loops.exactBeatLoopRangeMs(GRID, 1300, 1));
});

test('FXROLL-02 refuses short grids, bad lengths and bad tempo explicitly', () => {
	for (const beats of [[], [GRID[0]]]) {
		assert.throws(() => re.planRollEngage(input({ beats })), /at least 2 beats/);
	}
	for (const rollBeats of [3, 0.3, 64, 1 / 64, Number.NaN]) {
		assert.throws(() => re.planRollEngage(input({ rollBeats })), /1\/32\.\.32/);
	}
	for (const tempoRatio of [0, Number.NaN]) {
		assert.throws(() => re.planRollEngage(input({ tempoRatio })), /tempoRatio/);
	}
});

test('FXROLL-02 planned onset is within 1.0 ms of the grid slot, and wraps inside an engaged loop', () => {
	let seed = 7;
	const rand = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
	for (const rollBeats of [1 / 32, 1 / 16, 1 / 8, 1 / 4, 1 / 2, 1, 2, 4]) {
		for (let i = 0; i < 50; i++) {
			const p0 = rand() * 5;
			const ratio = 0.8 + rand() * 0.4;
			const plan = re.planRollEngage(
				input({ rollBeats, positionAtEarliestSec: p0, tempoRatio: ratio })
			);
			const onsetTrackSec = (plan.engageAtContextSec - 10) * ratio + p0;
			assert.ok(Math.abs(onsetTrackSec - plan.slotSec) * 48000 < 48);
			near(onsetTrackSec, plan.slotSec, 1e-6);
		}
	}
	const wrap = re.planRollEngage(
		input({
			rollBeats: 0.25,
			positionAtEarliestSec: 1.45,
			tempoRatio: 2,
			engagedLoop: { in_ms: 1000, out_ms: 1500 }
		})
	);
	near(wrap.engageAtContextSec, 10 + 0.05 / 2);
	near(wrap.playheadSec, 1.0);
	assert.throws(
		() =>
			re.planRollEngage(
				input({ positionAtEarliestSec: 1.02, engagedLoop: { in_ms: 1010, out_ms: 1060 } })
			),
		/1010\.\.1060ms/
	);
});
