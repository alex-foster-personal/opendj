/**
 * Cue-marker painting on the main waveform (issue #877): a hot cue, a loop
 * hot cue, and a memory cue must be visually separable, and a loop must
 * render as a spanning element rather than a point.
 *
 * Regression lines:
 * - if a non-loop hot cue and a loop hot cue paint the same fill colour then
 *   the two are indistinguishable again, which is half of issue #877
 * - if a loop cue's fillRect width collapses to a point (in_ms == out_ms
 *   pixel-wise) instead of spanning in to out then it is a point marker
 *   again, not the spanning element acceptance criterion 4 asks for
 * - if a memory cue ever gets the loop's orange/red-family fill then the
 *   hue reservation issue #877 asks for (orange/red = loops only) is broken
 * - if any cue marker paints with no stroke at all then it lost the outline
 *   that keeps it readable regardless of fill hue
 */
import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let render;

before(async () => {
	globalThis.Path2D = class {
		constructor() {
			this.rects = [];
		}
		rect(x, y, w, h) {
			this.rects.push({ x, y, w, h });
		}
	};
	render = await loadTypeScriptModule('src/lib/components/rb/wave/render.ts');
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

/** Records every fill/stroke call with the style in force at call time, plus
 * a single ordered `sequence` log (tag + style) shared across call kinds -
 * paint-order assertions (discussion_r3918219289) need calls of DIFFERENT
 * kinds (a loop's fillRect vs. the beat grid's fillRect vs. a phrase's
 * stroke) compared against each other, which the per-kind arrays below
 * cannot do alone. */
function recordingCtx() {
	const fills = [];
	const strokes = [];
	const fillRects = [];
	const strokeRects = [];
	const sequence = [];
	const ctx = {
		fillStyle: '',
		strokeStyle: '',
		lineWidth: 1,
		globalAlpha: 1,
		_path: [],
		beginPath() {
			ctx._path = [];
		},
		moveTo(x, y) {
			ctx._path.push(['move', x, y]);
		},
		lineTo(x, y) {
			ctx._path.push(['line', x, y]);
		},
		closePath() {
			ctx._path.push(['close']);
		},
		// Band painting calls fill(path2d); only the cue triangle calls the
		// argument-less fill() that reads the path built via moveTo/lineTo -
		// that distinction is what keeps this recorder from also picking up
		// every band fill drawn earlier in the same frame.
		fill(path) {
			if (path !== undefined) return;
			fills.push({ fillStyle: ctx.fillStyle, path: ctx._path.slice() });
			sequence.push({ kind: 'fill', style: ctx.fillStyle });
		},
		stroke(path) {
			if (path !== undefined) return;
			strokes.push({ strokeStyle: ctx.strokeStyle, path: ctx._path.slice() });
			sequence.push({ kind: 'stroke', style: ctx.strokeStyle });
		},
		fillRect(x, y, w, h) {
			fillRects.push({ x, y, w, h, fillStyle: ctx.fillStyle });
			sequence.push({ kind: 'fillRect', style: ctx.fillStyle });
		},
		strokeRect(x, y, w, h) {
			strokeRects.push({ x, y, w, h, strokeStyle: ctx.strokeStyle });
			sequence.push({ kind: 'strokeRect', style: ctx.strokeStyle });
		}
	};
	return { ctx, fills, strokes, fillRects, strokeRects, sequence };
}

function bands(n) {
	const values = Array.from({ length: n }, (_, i) => (i % 8) / 8);
	return { length: n, low: values, mid: values, high: values };
}

// WAVE_WINDOW_S is 24s; widthCss 240 gives 10px/s. positionMs 20000 centers
// the window on 8s..32s, so every cue below sits inside the visible strip.
const WIDTH = 240;
const HEIGHT = 60;
const POSITION_MS = 20_000;

function anlzWithCues(cues, { beats = [], phrases = [] } = {}) {
	return {
		stable_id: 'a'.repeat(40),
		points: 64,
		waveform: { kind: 'tri', preview: bands(64), detail: bands(64) },
		beatgrid: { source: 'rekordbox', beat_count: beats.length, beats },
		cues,
		phrases,
		vocals: { status: 'not_analyzed' }
	};
}

function paint(cues, gridOpts) {
	const rec = recordingCtx();
	render.drawWaveRow(rec.ctx, {
		widthCss: WIDTH,
		heightCss: HEIGHT,
		positionMs: POSITION_MS,
		durationMs: 300_000,
		anlz: anlzWithCues(cues, gridOpts),
		palette: PALETTE,
		pitch: 1,
		loop: null
	});
	return rec;
}

const HOT_CUE_A = {
	kind: 'hot_cue',
	slot: 'A',
	in_ms: 10_000,
	out_ms: null,
	is_loop: false,
	active_loop: false,
	beat_loop_size: null,
	color_table_index: null,
	comment: null
};

const LOOP_HOT_CUE_B = {
	kind: 'hot_cue',
	slot: 'B',
	in_ms: 15_000,
	out_ms: 17_000,
	is_loop: true,
	active_loop: false,
	beat_loop_size: 8,
	color_table_index: null,
	comment: null
};

const MEMORY_CUE = {
	kind: 'memory',
	slot: null,
	in_ms: 25_000,
	out_ms: null,
	is_loop: false,
	active_loop: false,
	beat_loop_size: null,
	color_table_index: null,
	comment: null
};

describe('wave cue markers', () => {
	it('a non-loop hot cue fills with cueHotCue and is stroked with cueOutline', () => {
		const { fills, strokes } = paint([HOT_CUE_A]);
		assert.equal(fills.length, 1);
		assert.equal(fills[0].fillStyle, PALETTE.cueHotCue);
		assert.equal(strokes.length, 1);
		assert.equal(strokes[0].strokeStyle, PALETTE.cueOutline);
	});

	it('a memory cue fills with cueMemory, never the loop/hot-cue colours', () => {
		const { fills } = paint([MEMORY_CUE]);
		assert.equal(fills.length, 1);
		assert.equal(fills[0].fillStyle, PALETTE.cueMemory);
		assert.notEqual(fills[0].fillStyle, PALETTE.cueLoop);
		assert.notEqual(fills[0].fillStyle, PALETTE.cueHotCue);
	});

	it('a loop hot cue renders as a spanning fillRect from in_ms to out_ms, not a point', () => {
		const { fillRects, fills, strokeRects } = paint([LOOP_HOT_CUE_B]);
		assert.equal(fills.length, 0, 'a loop must not also paint the point-marker triangle');
		// Other painters (row background, playhead) also call fillRect in the
		// same frame - isolate the loop's own band by its fill colour.
		const loopBands = fillRects.filter((r) => r.fillStyle === PALETTE.cueLoop);
		assert.equal(loopBands.length, 1);
		const band = loopBands[0];
		// in_ms 15s, out_ms 17s at 10px/s and window-left 8s -> x 70..90.
		assert.equal(band.x, 70);
		assert.ok(band.w >= 19, `loop band width ${band.w} collapsed toward a point`);
		const loopOutlines = strokeRects.filter((r) => r.strokeStyle === PALETTE.cueOutline);
		assert.equal(loopOutlines.length, 1);
	});

	it('a hot cue and a loop hot cue paint different fill colours (issue #877 separability)', () => {
		const { fills, fillRects } = paint([HOT_CUE_A, LOOP_HOT_CUE_B]);
		assert.equal(fills[0].fillStyle, PALETTE.cueHotCue);
		const loopBand = fillRects.find((r) => r.fillStyle === PALETTE.cueLoop);
		assert.ok(loopBand !== undefined, 'no fillRect painted with the loop colour');
		assert.notEqual(fills[0].fillStyle, loopBand.fillStyle);
	});

	it('the loop fill colour is reserved to loops: a non-loop hot cue never uses it', () => {
		const { fills } = paint([HOT_CUE_A]);
		assert.notEqual(fills[0].fillStyle, PALETTE.cueLoop);
	});

	it("a loop cue's band paints before the beat grid and phrases, not over them", () => {
		// LOOP_HOT_CUE_B spans 15s..17s; a beat at 16s and a phrase starting at
		// 16s both fall inside that span, so a pre-#877-style single paint pass
		// (loop band painted last) would blank both out under the opaque band.
		// shouldPaintBeatGrid requires validateBeatGrid-passing beats (>=2, cadence
		// 1,2,3,4); a single beat is not a usable PQTZ grid.
		const beats = [
			{ n: 1, bpm: 120, t: 16 },
			{ n: 2, bpm: 120, t: 16.5 }
		];
		const phrase = { start_s: 16, end_s: 20, kind: 1 };
		const { sequence } = paint([LOOP_HOT_CUE_B], { beats, phrases: [phrase] });

		const loopBandIndex = sequence.findIndex(
			(c) => c.kind === 'fillRect' && c.style === PALETTE.cueLoop
		);
		const beatTickIndex = sequence.findIndex(
			(c) => c.kind === 'fillRect' && c.style === PALETTE.tick
		);
		const phraseChevronIndex = sequence.findIndex(
			(c) => c.kind === 'stroke' && c.style === PALETTE.phrase
		);
		assert.ok(loopBandIndex >= 0, 'loop band never painted');
		assert.ok(beatTickIndex >= 0, 'beat tick never painted');
		assert.ok(phraseChevronIndex >= 0, 'phrase chevron never painted');
		assert.ok(
			loopBandIndex < beatTickIndex,
			'the loop band must paint before the beat grid, or it blanks out any tick under its span'
		);
		assert.ok(
			loopBandIndex < phraseChevronIndex,
			'the loop band must paint before phrase chevrons, or it blanks out any chevron under its span'
		);
	});
});
