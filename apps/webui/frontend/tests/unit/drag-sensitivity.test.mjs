import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// R5 (Wed 7 Oct 2026): base gain 0.7 of the old drag scale, integrated
// distance falloff gain(d) = base / (1 + d / D0), no jumps.

const m = await loadTypeScriptModule('src/lib/rb/drag-sensitivity.svelte.ts');
const MIXER = fileURLToPath(new URL('../../src/lib/components/rb/mixer', import.meta.url));

/** Drag in 1 px vertical steps from `fromPx` to `toPx` (UP distance from the press). */
function travel(fromPx, toPx, cfg = m.DRAG_DEFAULTS) {
	const t = m.createDragTracker(0, 0);
	let sum = 0;
	const dir = Math.sign(toPx - fromPx);
	// Get to the start without counting it.
	for (let y = 0; y !== fromPx; y += Math.sign(fromPx)) m.trackDragMove(t, 0, -(y + Math.sign(fromPx)), cfg);
	for (let y = fromPx; y !== toPx; y += dir) sum += m.trackDragMove(t, 0, -(y + dir), cfg).dy;
	return sum;
}

test('R5: the defaults are 0.7 base and a 150 px falloff', () => {
	assert.equal(m.DRAG_DEFAULTS.base, 0.7);
	assert.equal(m.DRAG_DEFAULTS.falloffPx, 150);
	assert.equal(m.dragSensitivity.base, 0.7);
});

test('R5: a 40 px drag near the press moves about 0.7x as far as before', () => {
	const near = travel(0, 40);
	const before = 40; // gain 1, no falloff
	assert.ok(near / before > 0.55 && near / before <= 0.7, `ratio ${near / before}`);
	const tiny = travel(0, 2);
	assert.ok(Math.abs(tiny / 2 - 0.7) < 0.01, `the base gain at d near 0 is ${tiny / 2}`);
});

test('R5: the same 40 px at d = 300 px moves clearly less', () => {
	const near = travel(0, 40);
	const far = travel(300, 340);
	assert.ok(far < near * 0.5, `far ${far} is not clearly less than near ${near}`);
});

test('R5: moving back toward the press restores fine control and the integral is path-exact', () => {
	const out = travel(0, 200);
	const back = travel(200, 0);
	assert.ok(Math.abs(out + back) < 1e-9, `out ${out} and back ${back} do not cancel`);
});

test('R5: no jump, each 1 px frame moves at most base px', () => {
	const t = m.createDragTracker(0, 0);
	for (let y = 1; y <= 400; y++) {
		const s = m.trackDragMove(t, 0, -y);
		assert.ok(s.dy > 0 && s.dy <= 0.7 + 1e-12, `frame ${y} moved ${s.dy}`);
	}
});

test('R5: the gain rejects nonsense config instead of guessing', () => {
	assert.throws(() => m.dragGain(10, -1, 150), RangeError);
	assert.throws(() => m.dragGain(10, 0.7, 0), RangeError);
});

test('R5 wiring: knob and fader drags (single and group) run through trackDragMove', () => {
	for (const f of ['Knob.svelte', 'VFader.svelte']) {
		const src = readFileSync(`${MIXER}/${f}`, 'utf8');
		assert.match(src, /trackDragMove\(dragTracker, e\.clientX, e\.clientY\)/, `${f} bypasses the gain`);
	}
	const fader = readFileSync(`${MIXER}/VFader.svelte`, 'utf8');
	const move = fader.slice(fader.indexOf('function handlePointerMove('), fader.indexOf('function handlePointerUp('));
	assert.doesNotMatch(move, /_valueFromEvent\(e\)/, 'a single fader drag still maps the pointer absolutely, skipping the gain');
});

test('an agent straight drag gains like a pointer drag and reads the live sensitivity', () => {
	// Sol P1, PR #6829: the knob bridge drag bypassed the drag gain, so an agent
	// drag ignored the user's sensitivity setting.
	const cfg = { base: 0.7, falloffPx: 150 };
	const t = m.createDragTracker(0, 0);
	let pointer = { dx: 0, dy: 0 };
	for (let i = 1; i <= 60; i += 1) {
		const s = m.trackDragMove(t, -i / 2, -i, cfg);
		pointer = { dx: pointer.dx + s.dx, dy: pointer.dy + s.dy };
	}
	const agent = m.gainedStraightDrag(-30, 60, cfg);
	assert.ok(Math.abs(agent.dx - pointer.dx) < 1e-9 && Math.abs(agent.dy - pointer.dy) < 1e-9,
		`agent ${JSON.stringify(agent)} vs pointer ${JSON.stringify(pointer)}`);
	assert.ok(agent.dy < 60 * 0.7, 'the falloff did not reduce a long drag');
	const prev = { base: m.dragSensitivity.base, falloff_px: m.dragSensitivity.falloffPx };
	const before = m.gainedStraightDrag(0, 20).dy;
	m.applyDragSensitivity({ base: prev.base * 2, falloff_px: prev.falloff_px });
	const after = m.gainedStraightDrag(0, 20).dy;
	m.applyDragSensitivity(prev);
	assert.ok(Math.abs(after / before - 2) < 1e-9, `doubling the base gave ${after / before}x`);
	assert.throws(() => m.gainedStraightDrag(Number.NaN, 0), RangeError);
});

test('a huge agent drag takes bounded steps and still matches the fine integral', () => {
	// Sol P2, PR #6829: one loop iteration per pixel with no bound.
	const t0 = performance.now();
	const huge = m.gainedStraightDrag(0, 1e12);
	assert.ok(performance.now() - t0 < 1000, 'a 1e12 px agent drag blocked the thread');
	assert.ok(Number.isFinite(huge.dy) && huge.dy > 0);
	// Control: at the bound the coarse and the 1 px integrals agree closely.
	const n = m.MAX_STRAIGHT_DRAG_STEPS;
	const atBound = m.gainedStraightDrag(0, n).dy;
	const coarse = m.gainedStraightDrag(0, n * 4).dy;
	const fine = atBound + travel(n, n * 4);
	assert.ok(Math.abs(coarse - fine) / fine < 0.01, `coarse ${coarse} vs fine ${fine}`);
});
