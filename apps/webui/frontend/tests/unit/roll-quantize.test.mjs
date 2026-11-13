// requirement: FXROLL-02
// Grid-boundary cases for the quantized roll engage planner ((private path), #13379).
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// 120 BPM, one beat every 0.5 s.
const GRID = Array.from({ length: 16 }, (_, i) => ({ n: (i % 4) + 1, bpm: 120, t: i * 0.5 }));
// 200 BPM with decimal beat times that binary floats cannot hold exactly.
const DECIMAL = [0, 0.3, 0.6, 0.9, 1.2, 1.5].map((t, i) => ({ n: (i % 4) + 1, bpm: 200, t }));
const UNEVEN = [
	{ n: 1, bpm: 120, t: 0 },
	{ n: 2, bpm: 120, t: 0.5 },
	{ n: 3, bpm: 100, t: 1.1 },
	{ n: 4, bpm: 100, t: 1.7 }
];
const LEAD_AT_SEC = 10;
const near = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`);

let re;
before(async () => {
	re = await loadTypeScriptModule('src/lib/player/transport/roll-engage.ts');
});

const plan = (over) =>
	re.planRollEngage({
		quantize: true,
		beats: GRID,
		rollBeats: 1,
		earliestWhenContextSec: LEAD_AT_SEC,
		positionAtEarliestSec: 1.3,
		tempoRatio: 1,
		currentPositionMs: 1300,
		engagedLoop: null,
		...over
	});

// [if] a request 1 us or 1 ms before a beat engages [then] it lands on that beat after the gap, [else stop].
test('FXROLL-02 request just before a beat boundary engages on that beat', () => {
	for (const gapSec of [1e-6, 1e-3]) {
		const p = plan({ positionAtEarliestSec: 1.5 - gapSec });
		assert.equal(p.kind, 'quantized');
		near(p.slotSec, 1.5);
		near(p.playheadSec, 1.5);
		near(p.engageAtContextSec, LEAD_AT_SEC + gapSec);
		assert.deepEqual(p.loop, { in_ms: 1500, out_ms: 2000 });
	}
	near(
		plan({ positionAtEarliestSec: 1.5 - 1e-3, tempoRatio: 2 }).engageAtContextSec,
		10 + 1e-3 / 2
	);
});

// [if] a request 1 us or 1 ms after a beat engages [then] it waits for the next beat, never the past one, [else stop].
test('FXROLL-02 request just after a beat boundary waits for the next beat', () => {
	for (const gapSec of [1e-6, 1e-3]) {
		const p = plan({ positionAtEarliestSec: 1.5 + gapSec });
		near(p.slotSec, 2.0);
		near(p.engageAtContextSec, LEAD_AT_SEC + 0.5 - gapSec);
		assert.deepEqual(p.loop, { in_ms: 2000, out_ms: 2500 });
		assert.ok(p.engageAtContextSec >= LEAD_AT_SEC);
	}
	const long = plan({ positionAtEarliestSec: 1.5 + 1e-6, rollBeats: 4 });
	near(long.slotSec, 2.0);
	assert.deepEqual(long.loop, { in_ms: 2000, out_ms: 4000 });
});

// [if] a 1/4 or 1/2 beat roll is requested near a slot edge [then] it engages on the next sub-beat slot, [else stop].
test('FXROLL-02 sub-beat grid engages on the next sub-beat slot either side of its edges', () => {
	const cases = [
		[0.25, 1.125 - 1e-6, 1.125],
		[0.25, 1.125 + 1e-6, 1.25],
		[0.25, 1.375 + 1e-6, 1.5],
		[1 / 32, 1.0 + 1e-6, 1.015625]
	];
	for (const [rollBeats, p0, slot] of cases) {
		const p = plan({ rollBeats, positionAtEarliestSec: p0 });
		near(p.slotSec, slot);
		near(p.engageAtContextSec, LEAD_AT_SEC + (slot - p0));
		near(p.loop.in_ms, slot * 1000, 1e-6);
		near(p.loop.out_ms - p.loop.in_ms, rollBeats * 500, 1e-6);
	}
	const uneven = plan({ beats: UNEVEN, rollBeats: 0.5, positionAtEarliestSec: 0.8 + 1e-6 });
	near(uneven.slotSec, 1.1);
	assert.deepEqual(uneven.loop, { in_ms: 1100, out_ms: 1400 });
	near(re.nextRollSlotSec(UNEVEN, 1.4 - 1e-6, 0.5), 1.4);
	assert.equal(re.nextRollSlotSec(UNEVEN, 1.4 + 1e-6, 0.5), null);
	assert.throws(
		() => plan({ beats: UNEVEN, rollBeats: 0.5, positionAtEarliestSec: 1.4 + 1e-6 }),
		/track end/
	);
});

// [if] the request sits a few ulps off a beat or slot [then] it engages on that slot with no negative wait, [else stop].
test('FXROLL-02 imprecise float boundary snaps to the slot, not the next one', () => {
	const cases = [
		[1, 0.1 * 3, 0.3],
		[1, 0.3 * 3, 0.9],
		[1, 0.9 + 4 * Number.EPSILON, 0.9],
		[0.125, 0.3 + 3 * 0.0375, 0.3 + 3 * ((0.6 - 0.3) / 8)],
		[0.125, 0.4125, 0.3 + 3 * ((0.6 - 0.3) / 8)]
	];
	for (const [rollBeats, p0, slot] of cases) {
		const p = plan({ beats: DECIMAL, rollBeats, positionAtEarliestSec: p0 });
		near(p.slotSec, slot, 1e-12);
		assert.ok(p.engageAtContextSec >= LEAD_AT_SEC);
		near(p.engageAtContextSec, LEAD_AT_SEC, 1e-12);
		near(p.loop.in_ms, slot * 1000, 1e-9);
	}
	const fromMs = plan({ positionAtEarliestSec: 1500.0000000000002 / 1000 });
	near(fromMs.slotSec, 1.5);
	near(fromMs.engageAtContextSec, LEAD_AT_SEC);
});
