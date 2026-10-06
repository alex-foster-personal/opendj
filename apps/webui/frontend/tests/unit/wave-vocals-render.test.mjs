import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// H11 - never fabricate a vocal answer. Vocal bars are painted only for status
// 'rekordbox' or 'demucs'; 'no_vocals' and 'not_analyzed' draw nothing; and a
// malformed vocals payload THROWS rather than rendering as "no vocals", which
// would tell the DJ the opposite of the truth. No test imported wave/render.ts
// at all before this file.
//
// Regression lines:
// - if not_analyzed or no_vocals paints any vocal pixel then broken
// - if a malformed vocals payload renders instead of throwing then broken
// - if bars stop landing at the region's own start_s/end_s then broken
// - if alpha stops rising with intensity then every passage reads equally strong
// - if demucs stops painting identically to rekordbox then the gap-fill is invisible

let render;
let api;

before(async () => {
	// Path2D is a browser API the band painter uses; node has none. A recording
	// stand-in keeps the real _drawBands path running instead of short-circuiting it.
	globalThis.Path2D = class {
		constructor() {
			this.rects = [];
		}
		rect(x, y, w, h) {
			this.rects.push({ x, y, w, h });
		}
	};
	render = await loadTypeScriptModule('src/lib/components/rb/wave/render.ts');
	api = await loadTypeScriptModule('src/lib/rb/api-rb.ts');
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
	phrase: '#888888',
	vocal: '#4fb2ff'
};

/** A recording 2D context: every fillRect kept with the style in force. */
function recordingCtx() {
	const calls = [];
	const ctx = {
		fillStyle: '',
		strokeStyle: '',
		lineWidth: 1,
		globalAlpha: 1,
		fillRect(x, y, w, h) {
			calls.push({ x, y, w, h, fillStyle: ctx.fillStyle, alpha: ctx.globalAlpha });
		},
		fill() {},
		beginPath() {},
		moveTo() {},
		lineTo() {},
		closePath() {},
		stroke() {}
	};
	return { ctx, calls };
}

/** Real-shaped band arrays so the waveform painter runs, not just the vocals. */
function bands(n) {
	const values = Array.from({ length: n }, (_, i) => (i % 8) / 8);
	return { length: n, low: values, mid: values, high: values };
}

/** A complete /anlz payload with the vocals field set to `vocals`. */
function anlzWith(vocals) {
	return {
		stable_id: 'a'.repeat(40),
		points: 64,
		waveform: { kind: 'tri', preview: bands(64), detail: bands(64) },
		beatgrid: { beat_count: 0, beats: [] },
		cues: [],
		phrases: [],
		vocals
	};
}

// Window maths: WAVE_WINDOW_S is 24s at pitch 1, centered on positionMs.
// positionMs 20000 puts the window at 8s..32s - deliberately NOT starting at 0,
// so a bar that ignores the window's left edge lands in the wrong place.
// widthCss 240 over a 24s window gives 10 px per second.
const WIDTH = 240;
const HEIGHT = 60;
const POSITION_MS = 20_000;
const WINDOW_LEFT_S = 8;
const PX_PER_S = 10;

function paint(vocals, positionMs = POSITION_MS) {
	const { ctx, calls } = recordingCtx();
	render.drawWaveRow(ctx, {
		widthCss: WIDTH,
		heightCss: HEIGHT,
		positionMs,
		durationMs: 300_000,
		anlz: anlzWith(vocals),
		palette: PALETTE,
		pitch: 1,
		loop: null
	});
	return calls.filter((c) => c.fillStyle === PALETTE.vocal);
}

// ------------------------------------------------ the four-state paint gate

test('not_analyzed paints zero vocal pixels', () => {
	assert.deepEqual(
		paint({ status: 'not_analyzed' }),
		[],
		'an unanalysed track must not get bars from stale or default regions'
	);
});

test('no_vocals paints zero vocal pixels', () => {
	assert.deepEqual(paint({ status: 'no_vocals', fps: 150, regions: [] }), []);
});

test('demucs regions paint bars at exactly their own start_s and end_s', () => {
	const regions = [
		{ start_s: 10, end_s: 13, intensity: 1 },
		{ start_s: 18, end_s: 22, intensity: 4 }
	];
	const rects = paint({ status: 'demucs', fps: 150, regions });
	assert.equal(rects.length, 2, `expected one bar per region, got ${rects.length}`);
	assert.deepEqual(
		rects.map((r) => [r.x, r.w]),
		regions.map((r) => [
			(r.start_s - WINDOW_LEFT_S) * PX_PER_S,
			(r.end_s - r.start_s) * PX_PER_S
		]),
		'bars must land on the regions relative to the visible window, not offset from them'
	);
	for (const rect of rects) {
		assert.equal(rect.y, 0, 'the vocal layer sits at the top of the row');
		assert.equal(rect.h, render.VOCAL_BAR_PX);
	}
});

test('scrolling the window moves the bar by exactly the elapsed distance', () => {
	// The bar is anchored to track time, so advancing the playhead by 1s must
	// slide it left by exactly PX_PER_S. A painter that forgets the window's
	// left edge leaves the bar pinned in place.
	const vocals = { status: 'demucs', fps: 150, regions: [{ start_s: 14, end_s: 16, intensity: 2 }] };
	const before = paint(vocals, POSITION_MS);
	const after = paint(vocals, POSITION_MS + 1000);
	assert.equal(before.length, 1);
	assert.equal(after.length, 1);
	assert.equal(
		before[0].x - after[0].x,
		PX_PER_S,
		`bar moved from x=${before[0].x} to x=${after[0].x} over 1s; expected a ${PX_PER_S}px slide`
	);
});

test('alpha rises with intensity rather than painting every passage the same', () => {
	const rects = paint({
		status: 'rekordbox',
		fps: 150,
		regions: [
			{ start_s: 9, end_s: 10, intensity: 1 },
			{ start_s: 11, end_s: 12, intensity: 2 },
			{ start_s: 13, end_s: 14, intensity: 3 },
			{ start_s: 15, end_s: 16, intensity: 4 }
		]
	});
	const alphas = rects.map((r) => r.alpha);
	assert.equal(alphas.length, 4);
	for (let i = 1; i < alphas.length; i++) {
		assert.ok(
			alphas[i] > alphas[i - 1],
			`alpha must rise with intensity, got ${JSON.stringify(alphas)}`
		);
	}
	assert.equal(alphas[0], 0.5, 'the weakest real region still reads at half opacity');
	assert.equal(alphas[3], 1);
});

test('rekordbox and demucs paint identically - the gap-fill is not second class', () => {
	const regions = [{ start_s: 10, end_s: 16, intensity: 3 }];
	const fromPvdi = paint({ status: 'rekordbox', fps: 150, regions });
	const fromDemucs = paint({ status: 'demucs', fps: 150, regions });
	assert.deepEqual(fromDemucs, fromPvdi);
	assert.equal(fromPvdi.length, 1);
});

test('regions outside the visible window paint nothing, not zero-width bars', () => {
	const rects = paint({
		status: 'demucs',
		fps: 150,
		regions: [
			{ start_s: 200, end_s: 210, intensity: 3 },
			{ start_s: -30, end_s: -20, intensity: 3 }
		]
	});
	assert.deepEqual(rects, []);
});

test('the vocal layer restores globalAlpha so later painters are not tinted', () => {
	const { ctx, calls } = recordingCtx();
	render.drawWaveRow(ctx, {
		widthCss: WIDTH,
		heightCss: HEIGHT,
		positionMs: POSITION_MS,
		durationMs: 300_000,
		anlz: anlzWith({ status: 'demucs', fps: 150, regions: [{ start_s: 10, end_s: 13, intensity: 1 }] }),
		palette: PALETTE,
		pitch: 1,
		loop: null
	});
	assert.equal(ctx.globalAlpha, 1);
	assert.ok(calls.length > 1, 'the frame must have painted more than the vocal layer');
});

// --------------------------------------------- a contract breach must throw

test('a malformed vocals payload throws instead of rendering as "no vocals"', () => {
	const breaches = [
		[undefined, /no valid "vocals" field/],
		[null, /no valid "vocals" field/],
		['rekordbox', /no valid "vocals" field/],
		[{ status: 'maybe', fps: 150, regions: [] }, /unknown status/],
		[{ status: 'demucs', regions: [] }, /fps missing/],
		[{ status: 'demucs', fps: 150 }, /regions missing/],
		[{ status: 'demucs', fps: 150, regions: [{ start_s: 1, end_s: 2 }] }, /region 0 malformed/],
		[
			{ status: 'demucs', fps: 150, regions: [{ start_s: 1, end_s: '2', intensity: 3 }] },
			/region 0 malformed/
		],
		[
			{ status: 'no_vocals', fps: 150, regions: [{ start_s: 1, end_s: 2, intensity: 3 }] },
			/no_vocals but regions non-empty/
		],
		[{ status: 'rekordbox', fps: 150, regions: [] }, /zero regions/]
	];
	for (const [vocals, pattern] of breaches) {
		assert.throws(
			() => paint(vocals),
			pattern,
			`a ${JSON.stringify(vocals)} payload must throw, not render`
		);
	}
});

test('an anlz payload with no vocals key at all throws at paint time', () => {
	const { ctx } = recordingCtx();
	const anlz = anlzWith({ status: 'not_analyzed' });
	delete anlz.vocals;
	assert.throws(
		() =>
			render.drawWaveRow(ctx, {
				widthCss: WIDTH,
				heightCss: HEIGHT,
				positionMs: POSITION_MS,
				durationMs: 300_000,
				anlz,
				palette: PALETTE,
				pitch: 1,
				loop: null
			}),
		/backend contract point 5 not met/
	);
});

// ------------------------------------------------------- the alpha ramp itself

test('vocalAlpha ramps 0.5..1.0 over intensity 1..4 and clamps outside it', () => {
	assert.equal(render.vocalAlpha(1), 0.5);
	assert.equal(render.vocalAlpha(4), 1);
	assert.ok(render.vocalAlpha(2) > 0.5 && render.vocalAlpha(2) < render.vocalAlpha(3));
	assert.equal(render.vocalAlpha(0), 0.5, 'out-of-range intensity clamps, never goes transparent');
	assert.equal(render.vocalAlpha(99), 1);
	for (const i of [1, 2, 3, 4]) {
		const a = render.vocalAlpha(i);
		assert.ok(a >= 0.5 && a <= 1, `alpha ${a} for intensity ${i} left the 0.5..1 ramp`);
	}
});

test('vocalsOf memoises per payload and still refuses a breach on the memo path', () => {
	const good = anlzWith({ status: 'demucs', fps: 150, regions: [] });
	assert.equal(api.vocalsOf(good), api.vocalsOf(good), 'memoised per payload object');
	assert.equal(api.vocalsOf(good).status, 'demucs');

	const bad = anlzWith({ status: 'nope' });
	assert.throws(() => api.vocalsOf(bad), /unknown status/);
	assert.throws(() => api.vocalsOf(bad), /unknown status/, 'a breach must not be memoised away');
});
