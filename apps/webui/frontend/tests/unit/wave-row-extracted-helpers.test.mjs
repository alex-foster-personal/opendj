/**
 * Pure helpers lifted out of WaveRow.svelte and routes/performance/+page.svelte
 * (quality ratchet, PR #3645) keep the exact behavior they had inline.
 *
 * - if the vocals tooltip drifts then a barless vocal state reads as ambiguous
 * - if tech-mode passthrough is not undone then another route inherits a
 *   transparent body
 * - if the repaint skip allocates per frame or stops swapping its slots then GC
 *   churns on every playing row, or a changed input is compared against itself
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let vocals;
let passthrough;
let paint;

before(async () => {
	vocals = await loadTypeScriptModule('src/lib/components/rb/wave/vocals-title.ts');
	passthrough = await loadTypeScriptModule('src/lib/rb/desktop-passthrough.ts');
	paint = await loadTypeScriptModule('src/lib/components/rb/wave/paint-position.ts');
});

function values(n, seed) {
	return Array.from({ length: n }, (_, i) => `${seed}-${i}`);
}

function writeAll(state, arr) {
	paint.writeVisualInputs(state, ...arr);
}

function anlzWithVocals(v) {
	const emptyBands = { length: 0, low: [], mid: [], high: [] };
	return {
		stable_id: 'abc123',
		points: 0,
		waveform: { kind: 'mono', preview: emptyBands, detail: emptyBands },
		beatgrid: { beat_count: 0, beats: [] },
		cues: [],
		phrases: [],
		vocals: v
	};
}

test('wave row vocals title names every barless state and stays quiet for rekordbox bars', () => {
	assert.equal(vocals.waveRowVocalsTitle(null), null);
	assert.equal(
		vocals.waveRowVocalsTitle(anlzWithVocals({ status: 'no_vocals', fps: 150, regions: [] })),
		'no vocals found'
	);
	assert.equal(
		vocals.waveRowVocalsTitle(anlzWithVocals({ status: 'not_analyzed' })),
		'vocals not analyzed yet'
	);
	assert.equal(
		vocals.waveRowVocalsTitle(
			anlzWithVocals({
				status: 'demucs',
				fps: 150,
				regions: [{ start_s: 1, end_s: 2, intensity: 1 }]
			})
		),
		'vocals: local detection'
	);
	assert.equal(
		vocals.waveRowVocalsTitle(
			anlzWithVocals({
				status: 'rekordbox',
				fps: 150,
				regions: [{ start_s: 1, end_s: 2, intensity: 1 }]
			})
		),
		null
	);
	assert.throws(
		() => vocals.waveRowVocalsTitle(anlzWithVocals({ status: 'bogus' })),
		/unknown status/
	);
});

function classHost() {
	const classes = new Set();
	return {
		classes,
		classList: {
			toggle(token, force) {
				if (force) classes.add(token);
				else classes.delete(token);
				return classes.has(token);
			},
			remove(token) {
				classes.delete(token);
			}
		}
	};
}

test('desktop passthrough follows tech mode on html and body, and its cleanup always clears it', () => {
	const html = classHost();
	const body = classHost();
	const cleanupOn = passthrough.applyDesktopPassthrough(true, [html, body]);
	assert.deepEqual([...html.classes], ['tw-desktop-passthrough']);
	assert.deepEqual([...body.classes], ['tw-desktop-passthrough']);
	cleanupOn();
	assert.equal(html.classes.size, 0);
	assert.equal(body.classes.size, 0);

	const cleanupOff = passthrough.applyDesktopPassthrough(false, [html, body]);
	assert.equal(html.classes.size, 0, 'inactive tech mode never adds the class');
	html.classes.add('tw-desktop-passthrough');
	cleanupOff();
	assert.equal(html.classes.size, 0, 'route teardown clears it even when set from elsewhere');
});

test('VISUAL_INPUT_COUNT is 33', () => {
	assert.equal(paint.VISUAL_INPUT_COUNT, 33);
});

test('shouldSkipRepaint keeps slot array identity across 1,000 unchanged frames', () => {
	const state = paint.initPaintScheduleState();
	const baseline = values(33, 'a');
	writeAll(state, baseline);
	assert.equal(paint.shouldSkipRepaint(state, false, 10), false);
	const inputsRef = state.inputs;
	const lastRef = state.lastInputs;
	for (let i = 0; i < 1000; i++) {
		writeAll(state, baseline);
		assert.equal(paint.shouldSkipRepaint(state, false, 10.4), true);
	}
	assert.equal(state.inputs, inputsRef);
	assert.equal(state.lastInputs, lastRef);
	assert.equal(state.inputs.length, paint.VISUAL_INPUT_COUNT);
	assert.equal(state.lastInputs.length, paint.VISUAL_INPUT_COUNT);
});

test('one changed visual input repaints and swaps the slot arrays', () => {
	const state = paint.initPaintScheduleState();
	const baseline = values(33, 'b');
	writeAll(state, baseline);
	paint.shouldSkipRepaint(state, false, 10);
	const capturedInputs = state.inputs;
	const capturedLast = state.lastInputs;
	const changed = baseline.slice();
	changed[7] = {};
	writeAll(state, changed);
	assert.equal(paint.shouldSkipRepaint(state, false, 10), false);
	assert.equal(state.lastInputs, capturedInputs);
	assert.equal(state.inputs, capturedLast);
	writeAll(state, changed);
	assert.equal(paint.shouldSkipRepaint(state, false, 10), true);
});

test('shouldSkipRepaint sub-pixel scroll, force, and Object.is semantics', () => {
	const state = paint.initPaintScheduleState();
	const baseline = values(33, 'c');
	const tick = (scrollPx, force = false) => {
		writeAll(state, baseline);
		return paint.shouldSkipRepaint(state, force, scrollPx);
	};
	assert.equal(tick(100), false);
	assert.equal(tick(99.01), true);
	assert.equal(tick(100.99), true);
	assert.equal(tick(101), false);
	assert.equal(tick(100, true), false);

	const nanState = paint.initPaintScheduleState();
	const withNan = values(33, 'd');
	withNan[0] = NaN;
	writeAll(nanState, withNan);
	paint.shouldSkipRepaint(nanState, false, 0);
	writeAll(nanState, withNan);
	assert.equal(paint.shouldSkipRepaint(nanState, false, 0), true);

	const zeroState = paint.initPaintScheduleState();
	const withZero = values(33, 'e');
	withZero[1] = 0;
	writeAll(zeroState, withZero);
	paint.shouldSkipRepaint(zeroState, false, 0);
	const withNegZero = withZero.slice();
	withNegZero[1] = -0;
	writeAll(zeroState, withNegZero);
	assert.equal(paint.shouldSkipRepaint(zeroState, false, 0), false);
});

test('writeVisualInputs throws naming the count when the argument count is wrong', () => {
	const state = paint.initPaintScheduleState();
	assert.throws(
		() => paint.writeVisualInputs(state, ...values(32, 'x')),
		/got 32 visual inputs, expected 33/
	);
	assert.throws(
		() => paint.writeVisualInputs(state, ...values(34, 'y')),
		/got 34 visual inputs, expected 33/
	);
});

test('shouldSkipRepaint throws when slot length does not match VISUAL_INPUT_COUNT', () => {
	const state = paint.initPaintScheduleState();
	state.inputs = values(32, 'short');
	assert.throws(() => paint.shouldSkipRepaint(state, false, 0), /expected 33/);
});
