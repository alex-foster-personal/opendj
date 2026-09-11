import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// DECKUX-04 - an engaged loop must be visible on EVERY waveform surface of the
// deck that owns it, not only the wavestack row. Reported by the maintainer on the
// /performance review widget, Mon 31 Aug 2026: "Loops not showing on waveforms
// other than primary". Confirmed live before the fix: engaging a loop repainted
// 3411 of 19584 sampled wavestack pixels and 0 of 3200 strip pixels.
//
// The two surfaces map track time to x completely differently (the row scrolls
// a 24s window, the strip spans the whole track), so this file paints the SAME
// loop through BOTH painters and asserts on the loop-colored fills each emits.
//
// Regression lines:
// - if an engaged loop paints on the wavestack row but not the deck strip then broken
// - if a strip loop band stops landing at the loop's own in_ms/out_ms then broken
// - if a loop shorter than the strip's pixel resolution renders zero-width then broken
// - if a non-engaged or absent loop paints any loop pixel then broken
// - if the two surfaces stop sharing one loop color then the loop reads as two features

let render;
let strip;
let waveMath;

before(async () => {
	// Path2D is a browser API the band painter uses; node has none. A recording
	// stand-in keeps the real band path running instead of short-circuiting it.
	globalThis.Path2D = class {
		constructor() {
			this.rects = [];
		}
		rect(x, y, w, h) {
			this.rects.push({ x, y, w, h });
		}
	};
	render = await loadTypeScriptModule('src/lib/components/rb/wave/render.ts');
	strip = await loadTypeScriptModule('src/lib/components/rb/deck/strip-waveform-render.ts');
	waveMath = await loadTypeScriptModule('src/lib/components/rb/wave/wave-math.ts');
});

const PALETTE = {
	bg: '#101010',
	low: '#e8a13a',
	mid: '#4f8fff',
	high: '#f0f0f0',
	tick: '#cccccc',
	cueHotCue: '#35c04f',
	cueLoop: '#e8a13a',
	cueMemory: '#ff3b30',
	cueOutline: '#c8cdd2',
	phrase: '#888888'
};

/** A recording 2D context: every fillRect kept with the style in force. */
function recordingCtx() {
	const calls = [];
	const ctx = {
		fillStyle: '',
		strokeStyle: '',
		lineWidth: 1,
		globalAlpha: 1,
		clearRect() {},
		fillRect(x, y, w, h) {
			calls.push({ x, y, w, h, fillStyle: ctx.fillStyle });
		},
		fill() {},
		beginPath() {},
		moveTo() {},
		lineTo() {},
		closePath() {},
		stroke() {},
		strokeRect() {}
	};
	return { ctx, calls };
}

/** Real-shaped band arrays so the waveform painter runs, not just the loop. */
function bands(n) {
	const values = Array.from({ length: n }, (_, i) => (i % 8) / 8);
	return { length: n, low: values, mid: values, high: values };
}

const WAVEFORM = { kind: 'tri', preview: bands(64), detail: bands(64) };

function anlz() {
	return {
		stable_id: 'a'.repeat(40),
		points: 64,
		waveform: WAVEFORM,
		beatgrid: { beat_count: 0, beats: [] },
		cues: [],
		phrases: [],
		vocals: { status: 'not_analyzed' }
	};
}

function engagedLoop(inMs, outMs) {
	return { in_ms: inMs, out_ms: outMs, engaged: true, beat_length: null };
}

// The loop band is the only thing painted in these two colors, so filtering on
// them separates it from the waveform's own orange lows (palette.low / BAND_LOW,
// both plain hex). Built from the exported constants, never retyped.
function loopFills(calls) {
	const fill = `rgba(${render.LOOP_ORANGE_RGB}, ${render.LOOP_FILL_ALPHA})`;
	const edge = `rgba(${render.LOOP_ORANGE_RGB}, ${render.LOOP_EDGE_ALPHA})`;
	return calls.filter((c) => c.fillStyle === fill || c.fillStyle === edge);
}

// ------------------------------------------------------------ the two surfaces

// One 300s track. Both surfaces below paint the SAME loop on it.
const DURATION_MS = 300_000;

// Wavestack row: WAVE_WINDOW_S is 24s at pitch 1, centered on positionMs.
// 75s puts the window at 63s..87s over 240px, i.e. 10 px per second.
const ROW_W = 240;
const ROW_H = 60;
const ROW_POSITION_MS = 75_000;

// Deck overview strip: the whole track across the 400px backing canvas,
// i.e. 400px / 300s. The same loop is 30x narrower here than on the row -
// which is exactly why it was possible to ship one surface without it.
const STRIP_W = 400;
const STRIP_H = 40;

function paintRow(loop) {
	const { ctx, calls } = recordingCtx();
	render.drawWaveRow(ctx, {
		widthCss: ROW_W,
		heightCss: ROW_H,
		positionMs: ROW_POSITION_MS,
		durationMs: DURATION_MS,
		anlz: anlz(),
		palette: PALETTE,
		pitch: 1,
		loop
	});
	return loopFills(calls);
}

function paintStripCalls(loop, { waveform = WAVEFORM, durationMs = DURATION_MS, loopCues = [] } = {}) {
	const { ctx, calls } = recordingCtx();
	strip.drawStripWaveform(ctx, {
		widthPx: STRIP_W,
		heightPx: STRIP_H,
		durationMs,
		waveform,
		vocals: null,
		loop,
		loopCues
	});
	return calls;
}

function paintStrip(loop, options) {
	return loopFills(paintStripCalls(loop, options));
}

test('an engaged loop paints on BOTH the wavestack row and the deck strip', () => {
	const loop = engagedLoop(70_000, 80_000);

	const rowFills = paintRow(loop);
	const stripFills = paintStrip(loop);

	assert.ok(
		rowFills.length > 0,
		'the wavestack row must paint the engaged loop (it always did - this guards the shared painter)'
	);
	assert.ok(
		stripFills.length > 0,
		'the deck overview strip must paint the engaged loop too; painting it on the row alone IS the DECKUX-04 defect'
	);
	// Both surfaces span their full height, so the loop reads as one feature.
	assert.ok(rowFills.every((c) => c.y === 0 && c.h === ROW_H));
	assert.ok(stripFills.every((c) => c.y === 0 && c.h === STRIP_H));
});

test('a stored loop hot cue does not hide the engaged-loop band on either waveform', () => {
	const loopCue = { in_ms: 70_000, out_ms: 80_000, beat_loop_size: 8 };
	const activeLoop = engagedLoop(loopCue.in_ms, loopCue.out_ms);

	const row = recordingCtx();
	render.drawWaveRow(row.ctx, {
		widthCss: ROW_W,
		heightCss: ROW_H,
		positionMs: ROW_POSITION_MS,
		durationMs: DURATION_MS,
		anlz: {
			...anlz(),
			beatgrid: {
				beat_count: 9,
				beats: Array.from({ length: 9 }, (_, i) => ({ n: (i % 4) + 1, bpm: 48, t: 70 + i * 1.25 }))
			},
			cues: [{ ...loopCue, kind: 'hot_cue', slot: 'B', is_loop: true }]
		},
		palette: PALETTE,
		pitch: 1,
		loop: activeLoop
	});
	const stripCalls = paintStripCalls(activeLoop, { waveform: null, loopCues: [loopCue] });
	const activeFill = `rgba(${render.LOOP_ORANGE_RGB}, ${render.LOOP_FILL_ALPHA})`;

	assert.ok(
		row.calls.some((call) => call.fillStyle === activeFill),
		'a loop-cue marker must not replace the main waveform active-loop band'
	);
	assert.ok(
		row.calls.some((call) => call.fillStyle === PALETTE.cueLoop),
		'a loop hot cue must span the main waveform marker strip'
	);
	const rowCue = row.calls.find((call) => call.fillStyle === PALETTE.cueLoop);
	assert.deepEqual(
		{ x: rowCue.x, w: rowCue.w },
		{ x: 70, w: 100 },
		'the 8-beat cue must end at its real beatgrid out_ms on the main waveform'
	);
	assert.ok(
		stripCalls.some((call) => call.fillStyle === activeFill),
		'a loop-cue marker must not replace the preview waveform active-loop band'
	);
	assert.ok(
		stripCalls.some((call) => call.fillStyle === strip.LOOP_CUE_COLOR),
		'a loop hot cue must span the preview waveform marker strip'
	);
	const stripCue = stripCalls.find((call) => call.fillStyle === strip.LOOP_CUE_COLOR);
	assert.ok(Math.abs(stripCue.x - (loopCue.in_ms / DURATION_MS) * STRIP_W) < 0.001);
	assert.ok(Math.abs(stripCue.w - ((loopCue.out_ms - loopCue.in_ms) / DURATION_MS) * STRIP_W) < 0.001);

	const disengagedCalls = paintStripCalls({ ...activeLoop, engaged: false }, { waveform: null, loopCues: [loopCue] });
	assert.ok(
		!disengagedCalls.some((call) => call.fillStyle === activeFill),
		'the preview active-loop band must disappear when the loop is disengaged'
	);
	assert.ok(
		disengagedCalls.some((call) => call.fillStyle === strip.LOOP_CUE_COLOR),
		'the stored loop-cue marker must remain when the active loop is disengaged'
	);
});

test('the strip loop band lands at the loop own in_ms and out_ms', () => {
	// 70s..80s of a 300s track over 400px = x 93.33..106.67.
	const fills = paintStrip(engagedLoop(70_000, 80_000));
	const left = Math.min(...fills.map((c) => c.x));
	const right = Math.max(...fills.map((c) => c.x + c.w));

	assert.ok(Math.abs(left - (70_000 / DURATION_MS) * STRIP_W) < 0.001, `left was ${left}`);
	assert.ok(Math.abs(right - (80_000 / DURATION_MS) * STRIP_W) < 0.001, `right was ${right}`);
});

test('a loop shorter than one strip pixel still renders visibly', () => {
	// 0.5s of a 300s track is 0.67px wide on the strip: without a floor it
	// rounds away to nothing and the DJ sees no loop at all.
	const TRUE_SPAN_PX = (500 / DURATION_MS) * STRIP_W;
	const fills = paintStrip(engagedLoop(60_000, 60_500));
	assert.ok(fills.length > 0, 'a sub-pixel loop must still paint');
	const left = Math.min(...fills.map((c) => c.x));
	const right = Math.max(...fills.map((c) => c.x + c.w));
	// Asserted against LITERAL 2, never against waveMath.LOOP_MIN_BAND_PX:
	// reading the constant the floor comes from makes the assertion true for
	// every value of it, 0 included, so the test would survive deletion of the
	// floor it exists to protect. Mutation-verified Mon 31 Aug 2026 - the
	// self-referential form passed with the floor set to 0.
	assert.ok(
		waveMath.LOOP_MIN_BAND_PX >= 2,
		`the visibility floor must stay at least 2px, was ${waveMath.LOOP_MIN_BAND_PX}`
	);
	assert.ok(right - left >= 2, `band was ${right - left}px, below the 2px visibility floor`);
	assert.ok(
		right - left > TRUE_SPAN_PX,
		'the floor must WIDEN a sub-pixel band, not merely pass its true span through'
	);
});

test('an engaged loop shows on the strip even when the track has no analysis', () => {
	// No ANLZ means no preview bands, but the loop is engine truth and the
	// strip rect is still on screen, so hiding it would be a lie.
	const fills = paintStrip(engagedLoop(70_000, 80_000), { waveform: null });
	assert.ok(fills.length > 0);
});

// ----------------------------------------------------------- nothing invented

test('no loop and a disengaged loop paint zero loop pixels on either surface', () => {
	const disengaged = { in_ms: 70_000, out_ms: 80_000, engaged: false, beat_length: null };

	assert.deepEqual(paintRow(null), [], 'row painted a loop that is not set');
	assert.deepEqual(paintStrip(null), [], 'strip painted a loop that is not set');
	assert.deepEqual(paintRow(disengaged), [], 'row painted a loop that is not engaged');
	assert.deepEqual(paintStrip(disengaged), [], 'strip painted a loop that is not engaged');
});

test('an empty deck paints no loop band - there is no duration to place it with', () => {
	assert.deepEqual(paintStrip(engagedLoop(70_000, 80_000), { durationMs: null }), []);
});

// -------------------------------------------------------- shared band geometry

test('loopBandPx clamps to the surface and drops a loop that is wholly outside', () => {
	const identity = (ms) => ms;
	const loop = engagedLoop(10, 90);

	assert.deepEqual(waveMath.loopBandPx(loop, identity, 100, 0), { left: 10, right: 90 });
	// Straddling both edges clamps to the surface, it does not disappear.
	assert.deepEqual(waveMath.loopBandPx(engagedLoop(-50, 150), identity, 100, 0), {
		left: 0,
		right: 100
	});
	// Entirely left of, and entirely right of, the surface.
	assert.equal(waveMath.loopBandPx(engagedLoop(-80, -10), identity, 100, 0), null);
	assert.equal(waveMath.loopBandPx(engagedLoop(120, 180), identity, 100, 0), null);
	// An empty or inverted span is not a loop.
	assert.equal(waveMath.loopBandPx(engagedLoop(50, 50), identity, 100, 0), null);
	assert.equal(waveMath.loopBandPx(engagedLoop(60, 40), identity, 100, 0), null);
});

test('loopBandPx keeps a min-width band inside the surface at either edge', () => {
	const identity = (ms) => ms;
	// A sub-pixel loop hard against the right edge widens INWARD, never past it.
	const band = waveMath.loopBandPx(engagedLoop(99.9, 100), identity, 100, 4);
	assert.deepEqual(band, { left: 96, right: 100 });
});

test('loopBandPx throws rather than painting garbage for a broken mapping', () => {
	assert.throws(
		() => waveMath.loopBandPx(engagedLoop(10, 90), () => Number.NaN, 100, 0),
		/finite px/
	);
});
