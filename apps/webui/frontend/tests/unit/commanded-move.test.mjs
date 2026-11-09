/** ANIM-9452 D1 commanded-move planner contract (#9452). */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const mod = await loadTypeScriptModule('src/lib/rb/commanded-move.ts');

const GRID_120 = Array.from({ length: 40 }, (_, i) => i * 500);

function plan(overrides = {}) {
	return mod.planCommandedMove({
		from: 0,
		to: 1,
		beats: 4,
		nowMs: 0,
		beatTimesMs: GRID_120,
		fallbackBeatMs: 500,
		...overrides
	});
}

test('120 BPM press 120 ms after a beat ends on the 4th strict boundary (~1880 ms later)', () => {
	const p = plan({ nowMs: 1120 });
	assert.equal(p.startMs, 1120);
	assert.equal(p.endMs, 3000);
	assert.ok(Math.abs(p.endMs - 1120 - 1880) < 0.5);
	assert.equal(p.endMs - 1120, 1880);
	assert.equal(p.gridless, false);
});

test('press exactly on a beat ends 4 full beats later (2000 ms at 120 BPM)', () => {
	const nowMs = 1000;
	const p = plan({ nowMs });
	assert.equal(p.endMs - nowMs, 2000);
	assert.equal(p.endMs, 3000);
	assert.equal(p.gridless, false);
});

test('beats zero jumps instantly with valueAt(nowMs) equal to to', () => {
	const p = mod.planCommandedMove({
		from: 0.2,
		to: 0.9,
		beats: 0,
		nowMs: 500,
		beatTimesMs: GRID_120,
		fallbackBeatMs: 500
	});
	assert.equal(p.endMs, p.startMs);
	assert.equal(p.startMs, 500);
	assert.equal(p.valueAt(500), 0.9);
	assert.equal(p.gridless, false);
});

test('empty grid uses fallback beat length and is gridless', () => {
	const p = plan({ beatTimesMs: [], nowMs: 120, beats: 4, fallbackBeatMs: 500 });
	assert.equal(p.endMs, 2120);
	assert.equal(p.gridless, true);
});

test('beatTimesMsFromGrid returns empty when validateBeatGrid fails and planner is gridless', () => {
	const bad = [
		{ n: 1, bpm: 120, t: 0 },
		{ n: 2, bpm: 120, t: 0.5 },
		{ n: 4, bpm: 120, t: 1.0 }
	];
	assert.deepEqual(mod.beatTimesMsFromGrid(bad), []);
	const p = plan({ beatTimesMs: mod.beatTimesMsFromGrid(bad), nowMs: 100, beats: 4 });
	assert.equal(p.gridless, true);
	assert.equal(p.endMs, 100 + 4 * 500);

	const valid = Array.from({ length: 8 }, (_, i) => ({
		n: (i % 4) + 1,
		bpm: 120,
		t: i * 0.5
	}));
	const ms = mod.beatTimesMsFromGrid(valid);
	assert.deepEqual(ms, [0, 500, 1000, 1500, 2000, 2500, 3000, 3500]);
	const onGrid = plan({
		beatTimesMs: ms,
		nowMs: 120,
		beats: 4,
		from: 0,
		to: 1
	});
	assert.equal(onGrid.gridless, false);
});

test('grid runs out before N boundaries after press falls back gridless', () => {
	const short = [0, 500, 1000, 1500];
	const p = plan({ beatTimesMs: short, nowMs: 600, beats: 4, fallbackBeatMs: 500 });
	assert.equal(p.gridless, true);
	assert.equal(p.endMs, 600 + 4 * 500);
});

test('non-increasing or NaN numeric grid is gridless', () => {
	const badOrder = plan({ beatTimesMs: [0, 500, 400, 1000], nowMs: 100 });
	assert.equal(badOrder.gridless, true);
	const nanGrid = plan({ beatTimesMs: [0, 500, NaN, 1500], nowMs: 100 });
	assert.equal(nanGrid.gridless, true);
});

test('100 linear samples are monotonic with exact endpoints (rising and falling)', () => {
	const rising = mod.planCommandedMove({
		from: 0.1,
		to: 0.8,
		beats: 4,
		nowMs: 0,
		beatTimesMs: GRID_120,
		fallbackBeatMs: 500,
		easing: 'linear'
	});
	const span = rising.endMs - rising.startMs;
	const values = [];
	for (let i = 0; i < 100; i++) {
		values.push(rising.valueAt(rising.startMs + (span * i) / 99));
	}
	for (let i = 1; i < values.length; i++) {
		assert.ok(values[i] >= values[i - 1]);
	}
	assert.equal(values[0], 0.1);
	assert.equal(values[99], 0.8);

	const falling = mod.planCommandedMove({
		from: 0.9,
		to: 0.2,
		beats: 4,
		nowMs: 0,
		beatTimesMs: GRID_120,
		fallbackBeatMs: 500,
		easing: 'linear'
	});
	const fspan = falling.endMs - falling.startMs;
	const fvals = [];
	for (let i = 0; i < 100; i++) {
		fvals.push(falling.valueAt(falling.startMs + (fspan * i) / 99));
	}
	for (let i = 1; i < fvals.length; i++) {
		assert.ok(fvals[i] <= fvals[i - 1]);
	}
	assert.equal(fvals[0], 0.9);
	assert.equal(fvals[99], 0.2);
});

test('ease-in-out hits midpoint, eases below linear at 25 percent, and stays monotonic', () => {
	const from = 0.1;
	const to = 0.9;
	const p = mod.planCommandedMove({
		from,
		to,
		beats: 4,
		nowMs: 0,
		beatTimesMs: GRID_120,
		fallbackBeatMs: 500,
		easing: 'ease-in-out'
	});
	const mid = (p.startMs + p.endMs) / 2;
	const midVal = p.valueAt(mid);
	assert.ok(Math.abs(midVal - (from + (to - from) * 0.5)) < 1e-12);
	const quarter = p.startMs + (p.endMs - p.startMs) * 0.25;
	const linearAtQuarter = from + (to - from) * 0.25;
	assert.ok(p.valueAt(quarter) < linearAtQuarter);
	assert.equal(p.valueAt(p.startMs), from);
	assert.equal(p.valueAt(p.endMs), to);
	const span = p.endMs - p.startMs;
	const samples = [];
	for (let i = 0; i < 100; i++) {
		samples.push(p.valueAt(p.startMs + (span * i) / 99));
	}
	for (let i = 1; i < samples.length; i++) {
		assert.ok(samples[i] >= samples[i - 1]);
	}
});

test('omitted easing defaults to linear', () => {
	const from = 0.2;
	const to = 0.8;
	const explicit = mod.planCommandedMove({
		from,
		to,
		beats: 4,
		nowMs: 0,
		beatTimesMs: GRID_120,
		fallbackBeatMs: 500,
		easing: 'linear'
	});
	const defaulted = mod.planCommandedMove({
		from,
		to,
		beats: 4,
		nowMs: 0,
		beatTimesMs: GRID_120,
		fallbackBeatMs: 500
	});
	const mid = (explicit.startMs + explicit.endMs) / 2;
	assert.equal(defaulted.valueAt(mid), explicit.valueAt(mid));
	const quarter = explicit.startMs + (explicit.endMs - explicit.startMs) * 0.25;
	assert.equal(defaulted.valueAt(quarter), explicit.valueAt(quarter));
});

test('out-of-range from and to clamp to 0..1', () => {
	const p = mod.planCommandedMove({
		from: -0.5,
		to: 1.7,
		beats: 4,
		nowMs: 0,
		beatTimesMs: GRID_120,
		fallbackBeatMs: 500,
		easing: 'linear'
	});
	assert.equal(p.valueAt(p.startMs), 0);
	assert.equal(p.valueAt(p.endMs), 1);
	const span = p.endMs - p.startMs;
	for (let i = 0; i <= 99; i++) {
		const v = p.valueAt(p.startMs + (span * i) / 99);
		assert.ok(v >= 0 && v <= 1);
	}
});

test('valueAt before start and after end hold from and to', () => {
	const p = plan({ from: 0.3, to: 0.7, nowMs: 1000 });
	assert.equal(p.valueAt(p.startMs - 1000), 0.3);
	assert.equal(p.valueAt(p.endMs + 1000), 0.7);
});

test('invalid inputs throw RangeError or TypeError before planning', () => {
	const base = {
		from: 0,
		to: 1,
		beats: 4,
		nowMs: 0,
		beatTimesMs: GRID_120,
		fallbackBeatMs: 500
	};
	assert.throws(() => mod.planCommandedMove({ ...base, beats: -1 }), RangeError);
	assert.throws(() => mod.planCommandedMove({ ...base, beats: 1.5 }), RangeError);
	assert.throws(() => mod.planCommandedMove({ ...base, nowMs: NaN }), RangeError);
	assert.throws(() => mod.planCommandedMove({ ...base, fallbackBeatMs: 0 }), RangeError);
	assert.throws(() => mod.planCommandedMove({ ...base, from: NaN }), RangeError);
	assert.throws(() => mod.planCommandedMove({ ...base, easing: 'bounce' }), RangeError);
	assert.throws(() => mod.planCommandedMove({ ...base, beatTimesMs: null }), TypeError);
	const planned = plan();
	assert.throws(() => planned.valueAt(NaN), RangeError);
});

test('module source has no svelte or DOM imports', () => {
	const src = readFileSync(new URL('../../src/lib/rb/commanded-move.ts', import.meta.url), 'utf8');
	const code = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '');
	assert.doesNotMatch(code, /from\s+['"]svelte/);
	assert.doesNotMatch(code, /\.svelte['"]/);
	assert.doesNotMatch(code, /\bwindow\b/);
	assert.doesNotMatch(code, /\bdocument\b/);
	assert.doesNotMatch(code, /\brequestAnimationFrame\b/);
	assert.doesNotMatch(code, /\bperformance\.now\b/);
	const importRe = /from\s+['"]([^'"]+)['"]/g;
	const allowed = new Set(['$lib/rb/beat-sync-math', '$lib/rb/anlz-types']);
	let m;
	while ((m = importRe.exec(code)) !== null) {
		assert.ok(allowed.has(m[1]), `unexpected import: ${m[1]}`);
	}
});

test('plan return shape fits D3 runner (startMs, endMs, valueAt)', () => {
	const p = plan();
	assert.equal(typeof p.startMs, 'number');
	assert.equal(typeof p.endMs, 'number');
	assert.equal(typeof p.valueAt, 'function');
	assert.equal(p.valueAt(p.startMs + 1), p.valueAt(p.startMs + 1));
});
