// requirement: WAVEQS-11733
// [if] half mode paints below the baseline, tops short of the row top, or full pixels drift [then] broken, [else stop].
// [if] the two height modes share a band cache key [then] broken, [else stop].
// requirement: WAVE-11732
// [if] rgbColumnColor maps a low-only column to anything but pure red, or a high-only column to anything but pure blue [then] broken.
// [if] three equal bands do not mix to a neutral grey [then] broken.
// [if] the rgb design paints a column at a height other than the tri-band union height [then] broken.
// [if] the per-band EQ layer path accepts the rgb design instead of failing loud [then] broken.
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let render;
let layers;
let MARKER_BAND_PX;

before(async () => {
	globalThis.Path2D = class {
		constructor() {
			this.rects = [];
			this.ops = [];
		}
		rect(x, y, w, h) {
			this.rects.push({ x, y, w, h });
		}
		moveTo(x, y) {
			this.ops.push({ x, y });
		}
		lineTo(x, y) {
			this.ops.push({ x, y });
		}
	};
	const cues = await loadTypeScriptModule('src/lib/components/rb/wave/cues.ts');
	MARKER_BAND_PX = cues.MARKER_BAND_PX;
	render = await loadTypeScriptModule('src/lib/components/rb/wave/render.ts');
	layers = await loadTypeScriptModule('src/lib/components/rb/wave/wave-band-layers.ts');
});

function recordingCtx() {
	const rects = [];
	const points = [];
	return {
		rects,
		points,
		fillStyle: '',
		globalAlpha: 1,
		fillRect(x, y, w, h) {
			rects.push({ x, y, w, h });
		},
		fill(path) {
			for (const r of path.rects) rects.push(r);
		},
		strokeStyle: '',
		lineWidth: 1,
		beginPath() {},
		moveTo() {},
		lineTo() {},
		stroke(path) {
			for (const p of path.ops) points.push(p);
		}
	};
}

/** A context recording each fill as (color, path). */
function recordingContext() {
	const fills = [];
	const target = {
		fillStyle: '',
		strokeStyle: '',
		globalAlpha: 1,
		fill(path) {
			fills.push({ color: String(target.fillStyle), path });
		}
	};
	const ctx = new Proxy(target, {
		get(t, key) {
			return key in t ? t[key] : () => {};
		},
		set(t, key, value) {
			t[key] = value;
			return true;
		}
	});
	return { ctx, fills };
}

const W = 64;
const H = 80;
const N = 64;
const loud = Array.from({ length: N }, () => 1);
const wfTri = {
	kind: 'tri',
	detail: { length: N, low: loud, mid: loud.map((v) => v * 0.5), high: loud.map((v) => v * 0.25) }
};
const wfMono = {
	kind: 'mono',
	detail: { length: N, low: loud, mid: loud, high: loud }
};
const palette = { low: '#111', mid: '#222', high: '#333', mono: '#ccc' };
const blocksSpec = { curve: 'true-peak', variant: 'max', beatPeriodS: 0.5, mirrored: true };
/** Full-scale low column in full tri-band mode (pinned geometry). */
const PINNED_FULL_LOW_COL0 = { x: 0, y: 11, w: 1, h: 68 };

function drawBands(design, waveform, waveHeight, blocks = undefined) {
	const ctx = recordingCtx();
	render.__test_drawBands(
		ctx,
		waveform,
		W,
		1,
		W,
		H,
		palette,
		design,
		blocks,
		MARKER_BAND_PX,
		null,
		null,
		waveHeight
	);
	return ctx;
}

function paintedRects(ctx) {
	return ctx.rects.filter((r) => r.h > 0);
}

function assertHalfRects(painted, label) {
	assert.ok(painted.length > 0, `${label}: expected painted bars`);
	for (const r of painted) {
		assert.ok(r.y >= MARKER_BAND_PX, `${label}: bar above marker band: ${JSON.stringify(r)}`);
		assert.ok(r.y + r.h <= H + 1e-9, `${label}: bar below baseline: ${JSON.stringify(r)}`);
		assert.equal(r.y + r.h, H, `${label}: bar bottom not on baseline: ${JSON.stringify(r)}`);
	}
}

test('WAVEQS-11733 half tri-band rises from the baseline and tops out under the marker band', () => {
	const painted = paintedRects(drawBands('tri-band', wfTri, 'half'));
	assertHalfRects(painted, 'tri-band half');
	const top = Math.min(...painted.map((r) => r.y));
	assert.equal(
		top,
		MARKER_BAND_PX + 1,
		`full-scale low should reach y=${MARKER_BAND_PX + 1}, got ${top}`
	);
});

test('WAVEQS-11733 half mono rises from the baseline and tops out under the marker band', () => {
	const painted = paintedRects(drawBands('mono', wfMono, 'half'));
	assertHalfRects(painted, 'mono half');
	const top = Math.min(...painted.map((r) => r.y));
	assert.equal(top, MARKER_BAND_PX + 1);
});

test('WAVEQS-11733 half line keeps vertices on or above the baseline', () => {
	const ctx = drawBands('line', wfTri, 'half');
	assert.ok(ctx.points.length > 0, 'line half paints vertices');
	for (const p of ctx.points) {
		assert.ok(p.y <= H + 1e-9, `line vertex below baseline: ${JSON.stringify(p)}`);
		assert.ok(p.y >= MARKER_BAND_PX, `line vertex in marker band: ${JSON.stringify(p)}`);
	}
	const top = Math.min(...ctx.points.map((p) => p.y));
	assert.equal(top, MARKER_BAND_PX + 1, 'full-scale line reaches the waveform top');
	assert.ok(
		ctx.points.some((p) => p.y === H),
		'line includes the bottom baseline'
	);
});

test('WAVEQS-11733 half blocks stack from the bottom baseline', () => {
	const painted = paintedRects(drawBands('blocks', wfTri, 'half', blocksSpec));
	assertHalfRects(painted, 'blocks half');
	const lows = painted.filter((r) => r.h === H - (MARKER_BAND_PX + 1));
	assert.ok(lows.length > 0, 'low band blocks reach the waveform top');
});

test('WAVEQS-11733 full mode matches default, stays symmetric, and pins the low column', () => {
	const centerY = MARKER_BAND_PX + (H - MARKER_BAND_PX) / 2;
	const implicit = paintedRects(drawBands('tri-band', wfTri, undefined));
	const explicit = paintedRects(drawBands('tri-band', wfTri, 'full'));
	assert.deepEqual(explicit, implicit);
	for (const r of explicit) {
		assert.equal(r.y + r.h / 2, centerY, `bar not centered: ${JSON.stringify(r)}`);
	}
	const col0Low = explicit.find((r) => r.x === 0 && r.h === PINNED_FULL_LOW_COL0.h);
	assert.deepEqual(col0Low, PINNED_FULL_LOW_COL0, 'full tri-band low column geometry drifted');
});

test('WAVEQS-11733 drawBandLayer half mode uses the bottom baseline', () => {
	const ctx = recordingCtx();
	layers.drawBandLayer(ctx, wfTri, W, H, palette, 'tri-band', 'low', 1, 0, W, 'half');
	const painted = paintedRects(ctx);
	assert.ok(painted.length > 0);
	for (const r of painted) assert.equal(r.y + r.h, H);
	assert.equal(Math.min(...painted.map((r) => r.y)), MARKER_BAND_PX + 1);
});

test('WAVEQS-11733 waveBandCacheKey differs between full and half', () => {
	const base = {
		design: 'tri-band',
		blocks: { curve: 'true-peak', variant: 'max', beatPeriodS: 0.5, mirrored: true },
		snapKey: 'u',
		w: W,
		h: H,
		palette,
		sectionKey: ''
	};
	const full = render.waveBandCacheKey({ ...base, waveHeight: 'full' });
	const half = render.waveBandCacheKey({ ...base, waveHeight: 'half' });
	assert.notEqual(full, half);
	assert.equal(render.waveBandCacheKey({ ...base, waveHeight: 'full' }), full);
});

/** Columns: low-only, high-only, all bands, silent, mixed. One point per column. */
function syntheticWaveform() {
	const low = [200, 0, 150, 0, 90];
	const mid = [0, 0, 150, 0, 40];
	const high = [0, 200, 150, 0, 120];
	const detail = { length: low.length, low, mid, high };
	return { kind: 'tri', preview: detail, detail };
}

const PALETTE = { low: '#111111', mid: '#222222', high: '#333333', mono: '#444444' };
const W_RGB = 5;
const H_RGB = 40;

/** Max painted rect height per column across every fill. */
function heightsByColumn(fills) {
	const out = new Array(W_RGB).fill(0);
	for (const { path } of fills) for (const r of path.rects) out[r.x] = Math.max(out[r.x], r.h);
	return out;
}

test('WAVE-11732 rgbColumnColor: pure primaries, neutral grey, silent null', () => {
	assert.equal(
		layers.rgbColumnColor({ low: 1, mid: 0, high: 0 }),
		'#ff0000',
		'[if] low-only is not pure red [then] broken'
	);
	assert.equal(
		layers.rgbColumnColor({ low: 0, mid: 1, high: 0 }),
		'#00ff00',
		'[if] mid-only is not pure green [then] broken'
	);
	assert.equal(
		layers.rgbColumnColor({ low: 0, mid: 0, high: 1 }),
		'#0000ff',
		'[if] high-only is not pure blue [then] broken'
	);
	const grey = layers.rgbColumnColor({ low: 0.4, mid: 0.4, high: 0.4 });
	const m = grey.match(/^#([0-9a-f]{2})\1\1$/);
	assert.ok(m, `[if] equal bands give ${grey}, not r = g = b [then] broken`);
	assert.ok(
		grey !== '#000000' && grey !== '#ffffff',
		'[if] equal bands give black or white [then] broken'
	);
	assert.equal(
		layers.rgbColumnColor({ low: 0, mid: 0, high: 0 }),
		null,
		'[if] a silent column gets a colour [then] broken'
	);
});

test('WAVE-11732 rgb design paints the tri-band union height in band-balance colours', () => {
	const wf = syntheticWaveform();
	const tri = recordingContext();
	render.drawWaveBands(tri.ctx, wf, W_RGB, H_RGB, PALETTE, 'tri-band', 0);
	const rgb = recordingContext();
	render.drawWaveBands(rgb.ctx, wf, W_RGB, H_RGB, PALETTE, 'rgb', 0);
	assert.deepEqual(
		heightsByColumn(rgb.fills),
		heightsByColumn(tri.fills),
		'[if] rgb height differs from the tri-band max [then] broken'
	);
	const colorAt = (x) =>
		rgb.fills.filter((f) => f.path.rects.some((r) => r.x === x)).map((f) => f.color);
	assert.deepEqual(colorAt(0), ['#ff0000'], '[if] a low-only column is not red [then] broken');
	assert.deepEqual(colorAt(1), ['#0000ff'], '[if] a high-only column is not blue [then] broken');
	assert.deepEqual(colorAt(3), [], '[if] a silent column paints [then] broken');
	for (const { color } of rgb.fills) {
		assert.ok(
			!Object.values(PALETTE).includes(color),
			`[if] rgb paints palette token ${color} [then] broken`
		);
	}
});

test('WAVE-11732 the per-band EQ layer path refuses the rgb design', () => {
	const { ctx } = recordingContext();
	assert.throws(
		() => layers.drawBandLayer(ctx, syntheticWaveform(), W_RGB, H_RGB, PALETTE, 'rgb', 'low', 1),
		/no per-band layers/,
		'[if] drawBandLayer paints rgb as separate bands [then] broken'
	);
});
