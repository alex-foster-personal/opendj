/**
 * Issue #10782 (S5): the edge sampler residency plan (spec 5.1, 5.3, 6): tier budgets, pin rules,
 * visibility promotion order, the closed-panel empty plan and pointer intent prefetch.
 *
 * Regression lines:
 * - if the perf tier is LOW, STANDARD or HIGH then the hot budget is 32, 64 or 128 MiB
 * - if a voice is armed or playing, a slot previews or a pad pins a slot then it is pinned; a free voice is not
 * - if the panel is open then visible slots plan hot in order before the neighbouring pages plan warm
 * - if the panel is closed and the pointer is idle then the plan is empty
 * - if the pointer heads for the edge fast enough then the first visible page plans hot with the panel closed
 * - if the pointer moves slowly or away from the edge then there is no intent
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let R;
before(async () => {
	R = await loadTypeScriptModule('src/lib/player/sampler/sampler-residency.ts');
});

const MiB = 1024 * 1024;
const s = (id) => ({ slot_id: id });

test('tier budgets', () => {
	/** [if] the perf tier is LOW, STANDARD or HIGH [then] the hot budget is 32, 64 or 128 MiB, [else stop]. */
	assert.equal(R.hotPoolBudgetBytes('LOW'), 32 * MiB);
	assert.equal(R.hotPoolBudgetBytes('STANDARD'), 64 * MiB);
	assert.equal(R.hotPoolBudgetBytes('HIGH'), 128 * MiB);
});

test('pins', () => {
	/** [if] voices are armed or playing, a slot previews and a pad pins one [then] all four pin, a free voice does not, [else stop]. */
	const voices = [
		{ slot_id: 'a', phase: 'armed' },
		{ slot_id: 'b', phase: 'playing' },
		{ slot_id: null, phase: 'free' }
	];
	assert.deepEqual([...R.residencyPins(voices, 'c', ['d'])].sort(), ['a', 'b', 'c', 'd']);
	assert.deepEqual([...R.residencyPins([], null, [])], []);
});

test('open panel promotes visible first', () => {
	/** [if] the panel is open [then] visible slots plan hot in visibility order, then neighbours warm, no repeats, [else stop]. */
	const plan = R.residencyPlan({
		open: true,
		intent: false,
		visible: [s('v1'), s('v2')],
		neighbours: [s('n1'), s('v2'), s('n2')]
	});
	assert.deepEqual(
		plan.map((p) => `${p.slot.slot_id}:${p.tier}`),
		['v1:hot', 'v2:hot', 'n1:warm', 'n2:warm']
	);
});

test('closed panel plans nothing', () => {
	/** [if] the panel is closed and the pointer is idle [then] the plan is empty, [else stop]. */
	assert.deepEqual(
		R.residencyPlan({ open: false, intent: false, visible: [s('v1')], neighbours: [s('n1')] }),
		[]
	);
});

test('intent promotes the first page hot', () => {
	/** [if] the panel is closed but the pointer heads for the edge [then] only the first visible page plans hot, [else stop]. */
	const plan = R.residencyPlan({
		open: false,
		intent: true,
		visible: [s('v1')],
		neighbours: [s('n1')]
	});
	assert.deepEqual(plan, [{ slot: s('v1'), tier: 'hot' }]);
});

// 1000 px wide viewport, 24 px edge zone; points every 16 ms (one frame).
const path = (x0, pxPerMs, n = 8) =>
	Array.from({ length: n }, (_, i) => ({ t: i * 16, x: x0 + pxPerMs * i * 16 }));

test('pointer intent', () => {
	/** [if] the pointer will reach the edge zone within 120 ms [then] intent; slow, away or too few points do not, [else stop]. */
	// 300 px from the right edge, 3 px/ms: 360 px in 120 ms reaches the zone
	assert.equal(R.pointerIntent(path(600, 3), 'R', 1000, 24), true);
	// the same distance at 1 px/ms falls short
	assert.equal(R.pointerIntent(path(600, 1), 'R', 1000, 24), false);
	// moving away from the right edge
	assert.equal(R.pointerIntent(path(900, -3), 'R', 1000, 24), false);
	// toward the left edge
	assert.equal(R.pointerIntent(path(400, -3), 'L', 1000, 24), true);
	assert.equal(R.pointerIntent([{ t: 0, x: 990 }], 'R', 1000, 24), false);
	// only the last 8 points count: an old slow stretch does not damp a fresh fast move
	const mixed = [...path(0, 0.1, 20).map((p) => ({ ...p, t: p.t - 400 })), ...path(600, 3)];
	assert.equal(R.pointerIntent(mixed, 'R', 1000, 24), true);
});
