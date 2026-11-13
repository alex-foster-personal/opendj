// requirement: PGWAVE-7692b
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// PERF-PAINT-01 regression lines:
// - if a sub-pixel playhead advance rebuilds the band image then broken
// - if a one-pixel advance does not move the cached band image then broken
// - if width or zoom changes reuse the old band image then broken
// PERF-PAINT-10529 regression lines:
// - if a row-height drag rebuilds band tiles per pixel of height then broken
// - if a height move past HEIGHT_REUSE_RATIO is drawn from stretched tiles then broken
// - if a row that has held its new height for HEIGHT_SETTLE_FRAMES draws is still scaled then broken

let render;
let createdCanvases = 0;
/** Every band tile canvas built, with the block rects painted into it. */
let tiles = [];

class RecordingPath {
	constructor() {
		this.rects = [];
	}
	rect(x, y, w, h) {
		this.rects.push({ x, y, w, h });
	}
}

function context(calls, smoothLog = null) {
	return {
		fillStyle: '',
		strokeStyle: '',
		lineWidth: 1,
		globalAlpha: 1,
		imageSmoothingEnabled: true,
		fillRect() {},
		fill() {},
		drawImage(...args) {
			calls.push(args);
			if (smoothLog !== null) smoothLog.push(this.imageSmoothingEnabled);
		},
		beginPath() {},
		moveTo() {},
		lineTo() {},
		closePath() {},
		stroke() {}
	};
}

before(async () => {
	globalThis.Path2D = RecordingPath;
	globalThis.document = {
		createElement(tag) {
			assert.equal(tag, 'canvas');
			createdCanvases += 1;
			const calls = [];
			const canvas = { width: 0, height: 0, rects: [] };
			const ctx = context(calls);
			ctx.fill = (path) => canvas.rects.push(...path.rects);
			// Block bars paint with fillRect since #7692 (render.ts _drawBands; WebKit
			// antialiases a Path2D fill of 2px bars), not Path2D. Record them too so
			// paintedEdges() sees the real painted layout. Only full-alpha fills are
			// bars; the upper band and beat lines paint at other alphas.
			ctx.fillRect = (x, y, w, h) => {
				if (ctx.globalAlpha === 1) canvas.rects.push({ x, y, w, h });
			};
			canvas.getContext = () => ctx;
			tiles.push(canvas);
			return canvas;
		}
	};
	render = await loadTypeScriptModule('src/lib/components/rb/wave/render.ts');
	// The snapper is a lazy chunk; load it so snapped rows paint snapped bars.
	await render.__test_loadBlockSnap();
});

const PALETTE = {
	bg: '#101010',
	low: '#e8a13a',
	mid: '#4f8fff',
	high: '#f0f0f0',
	tick: '#ccc',
	cueHotCue: '#35c04f',
	cueLoop: '#e8a13a',
	cueMemory: '#ff3b30',
	cueOutline: '#c8cdd2',
	phrase: '#888'
};

const detail = {
	length: 1200,
	low: Array(1200).fill(0.5),
	mid: Array(1200).fill(0.5),
	high: Array(1200).fill(0.5)
};
const anlz = {
	stable_id: 'a'.repeat(40),
	points: 1200,
	waveform: { kind: 'tri', preview: detail, detail },
	beatgrid: { beat_count: 0, beats: [] },
	cues: [],
	phrases: [],
	vocals: { status: 'not_analyzed' }
};

function frame(positionMs, widthCss = 240, pitch = 1, heightCss = 60) {
	return {
		widthCss,
		heightCss,
		positionMs,
		durationMs: 120_000,
		anlz,
		palette: PALETTE,
		pitch,
		loop: null,
		windowS: 24,
		playheadFrac: 0.5
	};
}

test('sub-pixel advances blit the cached band image without rebuilding its buckets', () => {
	render.resetWaveBandCacheForTest();
	createdCanvases = 0;
	const calls = [];
	const ctx = context(calls);
	render.drawWaveRow(ctx, frame(30_000));
	const firstBuilds = createdCanvases;
	render.drawWaveRow(ctx, frame(30_050)); // 0.5 CSS px at 10px/s
	assert.equal(
		createdCanvases,
		firstBuilds,
		'a move below one CSS pixel must not rescan or rebuild bands'
	);
	assert.equal(calls.length, 2, 'each frame blits the already-built band image');
});

test('cache keys include width and zoom so stale bands cannot be stretched', () => {
	render.resetWaveBandCacheForTest();
	createdCanvases = 0;
	const ctx = context([]);
	render.drawWaveRow(ctx, frame(30_000));
	render.drawWaveRow(ctx, frame(30_000, 480));
	render.drawWaveRow(ctx, frame(30_000, 480, 1.1));
	assert.equal(createdCanvases, 3, 'waveform, zoom, and width each select their own band image');
});

// PERF-GRID-02 regression lines:
// - if a Beat Sync tempo nudge rebuilds the band image then broken
// - if the reused image is drawn off the playhead at the nudged scale then broken
// - if a real tempo move (beyond PITCH_TOLERANCE) reuses a stretched image then broken
// Measured on demon-llama, Mon 5 Oct 2026: two synced decks under tempo nudges
// rebuilt the whole-track band image 17 times in 10 s (3.2 fps).

test('Beat Sync tempo nudges reuse one band image', () => {
	render.resetWaveBandCacheForTest();
	createdCanvases = 0;
	const ctx = context([]);
	for (let i = 0; i < 30; i++) {
		render.drawWaveRow(ctx, frame(30_000 + i * 16, 240, 1.0161 + (i % 7) * 0.0003));
	}
	assert.equal(createdCanvases, 1, 'a follower nudged by the phase lock keeps its band image');
});

test('a scaled band image still puts the playhead track time at the row centre', () => {
	render.resetWaveBandCacheForTest();
	const calls = [];
	const ctx = context(calls);
	render.drawWaveRow(ctx, frame(30_000, 240, 1.0));
	render.drawWaveRow(ctx, frame(30_000, 240, 1.02));
	const args = calls.at(-1);
	assert.equal(args.length, 9, 'a nudged frame blits a scaled source rect');
	const [, sx, , sw, , dx, , dw] = args;
	const builtPxPerS = 240 / render.WAVE_WINDOW_S; // the image was built at pitch 1.0
	const sourceX = 30 * builtPxPerS;
	const destX = dx + ((sourceX - sx) * dw) / sw;
	// The blit snaps to a device pixel so scrolling bars are not resampled.
	// Half a CSS pixel is the most that snap may move the playhead sample.
	assert.ok(Math.abs(destX - 120) < 0.5, `playhead maps to x=${destX}, expected 120`);
	const liveScale = dw / sw;
	assert.ok(Math.abs(liveScale - 1 / 1.02) < 1e-9, 'drawn at the live px-per-second');
});

test('a tempo move beyond the tolerance rebuilds the band image', () => {
	render.resetWaveBandCacheForTest();
	createdCanvases = 0;
	const ctx = context([]);
	render.drawWaveRow(ctx, frame(30_000, 240, 1.0));
	render.drawWaveRow(ctx, frame(30_000, 240, 1.05));
	assert.equal(createdCanvases, 2);
});

test('blocks scroll lands on whole pixels and does not filter the bars', () => {
	render.resetWaveBandCacheForTest();
	const calls = [];
	const smooth = [];
	const ctx = context(calls, smooth);
	const blocks = (positionMs) => ({ ...frame(positionMs), waveformDesign: 'blocks' });
	render.drawWaveRow(ctx, blocks(30_000));
	render.drawWaveRow(ctx, blocks(30_010)); // 0.1 CSS px at 10px/s
	assert.deepEqual(smooth, [false, false], 'the bar blit is nearest-neighbor');
	assert.equal(ctx.imageSmoothingEnabled, true, 'smoothing is restored for the overlays');
	// Main blits the band image as tiles: an unscaled tile is the 3-argument
	// drawImage form (dest x at args[1]), a scaled one the 9-argument form
	// (dest x at args[5]).
	const dx = (args) => (args.length === 3 ? args[1] : args[5]);
	assert.equal(calls.length, 2, 'one tile blit per frame at this width');
	assert.equal(dx(calls[0]), dx(calls[1]), 'a sub-pixel advance stays on the snapped pixel');
	assert.equal(
		Number.isInteger(dx(calls[0])),
		true,
		'the snapped dest x is a whole CSS pixel at dpr 1'
	);
});

test('the reuse tolerance is PITCH_TOLERANCE on either side', () => {
	const tol = render.BAND_CACHE_CFG.PITCH_TOLERANCE;
	assert.equal(render.bandImageScaleReusable(10, 10 * (1 + tol - 1e-9)), true);
	assert.equal(render.bandImageScaleReusable(10, 10 * (1 + tol + 1e-6)), false);
	assert.equal(render.bandImageScaleReusable(10, 10 * (1 - tol + 1e-9)), true);
	assert.equal(render.bandImageScaleReusable(10, 10 * (1 - tol - 1e-6)), false);
});

// DECKUX-42 regression lines (Sol P1 4303186414 on PR #6534):
// - if a snapped Gothic row blits its band image at a fractional offset then broken
// - if a beat line and its snapped block edge land on different pixels while scrolling then broken
// - if a design with no snapped edges loses its sub-pixel scroll then broken (overshoot control)

// One grid object for every frame: a new beats array is a grid edit, which
// rebuilds a snapped row's image (beatsVersion).
const GRID_BEATS = Array.from({ length: 200 }, (_, i) => ({
	n: (i % 4) + 1,
	bpm: 120,
	t: 0.513 + i * 0.5
}));
const GRID_ANLZ = { ...anlz, beatgrid: { beat_count: GRID_BEATS.length, beats: GRID_BEATS } };

// 20 px per beat, as on a deck: under BEAT_LINE_MIN_GAP_PX the grid thins to
// bars (WAVEZOOM-02) and would no longer paint a line on every beat edge.
const GRID_W = 960;

function gridFrame(positionMs, design) {
	return {
		...frame(positionMs, GRID_W),
		anlz: GRID_ANLZ,
		waveformDesign: design,
		blocksSnap: true
	};
}

/** Left edges, in image px, of the block bars painted into tile 0 since the
 * last reset: the real painted layout, not a recomputed one. */
function paintedEdges() {
	assert.equal(tiles.length, 1, 'CONTROL: the window paints from one band tile');
	return new Set(tiles[0].rects.map((r) => r.x));
}

function recordingContext() {
	const blits = [];
	const lines = [];
	const ctx = context(blits);
	ctx.fillRect = (x, y, w, h) => {
		// Beat-line bodies are the only full-height rects painted at these alphas.
		if (ctx.globalAlpha === 0.38 || ctx.globalAlpha === 0.55) lines.push(x);
	};
	return { ctx, blits, lines };
}

test('a snapped Gothic row keeps every beat edge on its grid line while scrolling', () => {
	const pxPerS = GRID_W / render.WAVE_WINDOW_S;
	let fractionalSeen = 0;
	for (let ms = 30_000; ms < 30_400; ms += 7) {
		render.resetWaveBandCacheForTest();
		tiles = [];
		const f = gridFrame(ms, 'blocks');
		const { ctx, blits, lines } = recordingContext();
		render.drawWaveRow(ctx, f);
		const dx = blits.at(-1)[1];
		assert.ok(Number.isInteger(dx), `blit offset ${dx} at ${ms} ms is a whole pixel`);
		const tLeft = ms / 1000 - render.WAVE_WINDOW_S / 2;
		if (!Number.isInteger(tLeft * pxPerS)) fractionalSeen += 1;
		const visible = f.anlz.beatgrid.beats.filter(
			(b) => b.t >= tLeft && b.t <= tLeft + render.WAVE_WINDOW_S
		);
		assert.equal(lines.length, visible.length, 'CONTROL: every visible beat paints a line');
		const edges = paintedEdges();
		for (const x of lines)
			assert.ok(edges.has(x - dx), `a painted bar starts on the line at ${x} px, ${ms} ms`);
	}
	assert.ok(fractionalSeen > 0, 'CONTROL: the sweep crosses fractional scroll offsets');
});

test('designs without snapped edges keep the tile blit rounding', () => {
	render.resetWaveBandCacheForTest();
	const { ctx, blits } = recordingContext();
	const ms = 30_007;
	render.drawWaveRow(ctx, gridFrame(ms, 'tri-band'));
	const tLeft = ms / 1000 - render.WAVE_WINDOW_S / 2;
	const exact = -tLeft * (GRID_W / render.WAVE_WINDOW_S);
	assert.ok(!Number.isInteger(exact), 'CONTROL: this position is a fractional offset');
	const expected = Math.round(exact);
	assert.equal(blits.at(-1)[1], expected);
});

// DECKUX-42 Beat Sync bound (Sol P1 4303506950 on PR #6534): while a nudge
// reuses an image built at another scale, the edge is rounded at the built
// scale and drawn scaled, the line rounded at the live one. Three half-pixel
// roundings, scaled by at most PITCH_TOLERANCE: under 1.52 px, never more.
test('a snapped row under a Beat Sync nudge keeps each beat edge within 1.52 px of its line', () => {
	const bound = 1 + 0.5 * (1 + render.BAND_CACHE_CFG.PITCH_TOLERANCE);
	assert.ok(bound < 1.52);
	let worst = 0;
	for (const pitch of [0.975, 0.99, 1.01, 1.025]) {
		for (let ms = 30_000; ms < 30_400; ms += 13) {
			render.resetWaveBandCacheForTest();
			tiles = [];
			const built = recordingContext();
			render.drawWaveRow(built.ctx, { ...gridFrame(ms, 'blocks'), pitch: 1 });
			const { ctx, blits, lines } = recordingContext();
			const f = { ...gridFrame(ms, 'blocks'), pitch };
			render.drawWaveRow(ctx, f);
			const args = blits.at(-1);
			assert.equal(args.length, 9, 'CONTROL: the nudged frame reuses the image scaled');
			const [, sx, , sw, , dx, , dw] = args;
			const builtPxPerS = GRID_W / render.WAVE_WINDOW_S;
			const trackWindowS = render.WAVE_WINDOW_S * pitch;
			const tLeft = ms / 1000 - trackWindowS / 2;
			const visible = f.anlz.beatgrid.beats.filter(
				(b) => b.t >= tLeft && b.t <= tLeft + trackWindowS
			);
			assert.equal(lines.length, visible.length, 'CONTROL: one line per visible beat');
			const painted = paintedEdges();
			for (let i = 0; i < visible.length; i++) {
				// The bar the image painted for this beat, then where the scaled blit puts it.
				const edge = Math.round(visible[i].t * builtPxPerS);
				assert.ok(painted.has(edge), `a painted bar starts at the beat's image px ${edge}`);
				worst = Math.max(worst, Math.abs(lines[i] - (dx + ((edge - sx) * dw) / sw)));
			}
		}
	}
	assert.ok(worst > 0.5, `CONTROL: the sweep reaches the rounding error (worst ${worst})`);
	assert.ok(worst < bound, `worst ${worst.toFixed(3)} px against ${bound.toFixed(3)} px`);
});

test('a pitch nudge that crosses the 10 px thinning threshold rebuilds the band image', () => {
	const windowS = 24;
	const widthCss = 480;
	const pitch198 = 20 / 19.8;
	const pitch201 = 20 / 20.1;
	render.resetWaveBandCacheForTest();
	createdCanvases = 0;
	tiles = [];
	const { ctx: ctx1 } = recordingContext();
	render.drawWaveRow(ctx1, { ...gridFrame(30_000, 'blocks'), widthCss, windowS, pitch: pitch198 });
	const afterFirst = createdCanvases;
	tiles = [];
	const rec2 = recordingContext();
	render.drawWaveRow(rec2.ctx, {
		...gridFrame(30_000, 'blocks'),
		widthCss,
		windowS,
		pitch: pitch201
	});
	assert.ok(createdCanvases > afterFirst, 'stride change inside PITCH_TOLERANCE must rebuild');
	const dx = rec2.blits.at(-1)[1];
	const edges = new Set(tiles[0].rects.map((r) => r.x));
	for (const x of rec2.lines) assert.ok(edges.has(x - dx), `rebuilt layout snaps line at ${x} px`);
	render.resetWaveBandCacheForTest();
	createdCanvases = 0;
	tiles = [];
	render.drawWaveRow(recordingContext().ctx, {
		...gridFrame(30_000, 'blocks'),
		widthCss,
		windowS,
		pitch: 20 / 19.7
	});
	render.drawWaveRow(recordingContext().ctx, {
		...gridFrame(30_000, 'blocks'),
		widthCss,
		windowS,
		pitch: 20 / 19.9
	});
	assert.equal(createdCanvases, 1, 'two nudges in the same stride band still build once');
});

// PERF-PAINT-10529 (#10218): row height reuses tiles by a scaled blit.
function resetCounts() {
	render.resetWaveBandCacheForTest();
	createdCanvases = 0;
	tiles = [];
}

function sweep(heights, extra = {}) {
	const calls = [];
	for (const hh of heights)
		render.drawWaveRow(context(calls), { ...frame(30_000, 240, 1, hh), ...extra });
	return calls;
}

function range(from, to) {
	const step = to >= from ? 1 : -1;
	const out = [];
	for (let v = from; v !== to + step; v += step) out.push(v);
	return out;
}

test('a row-height drag inside the reuse ratio builds tiles at the start and after it settles', () => {
	const settle = render.BAND_CACHE_CFG.HEIGHT_SETTLE_FRAMES;
	const drag = [...range(100, 150), ...range(149, 120)];
	for (const [extra, perBuild] of [
		[{}, 1],
		[{ bandGains: { low: 0.5, mid: 1, high: 1 } }, 3]
	]) {
		resetCounts();
		sweep(drag, extra);
		assert.equal(createdCanvases, perBuild, 'today: one tile set per distinct height');
		sweep(Array(settle - 1).fill(120), extra);
		assert.equal(createdCanvases, 2 * perBuild, 'a settled height rebuilds once');
		assert.equal(tiles.at(-1).height, 120);
	}
});

test('the issue sweep 100 to 160 and back builds three times, not once per height', () => {
	resetCounts();
	const settle = render.BAND_CACHE_CFG.HEIGHT_SETTLE_FRAMES;
	const heights = [...range(100, 160), ...range(159, 100), ...Array(settle - 1).fill(100)];
	sweep(heights);
	// 151 is 0.51 of 100, past the 0.5 HEIGHT_REUSE_RATIO, so the sweep rebuilds
	// once there; every height back down to 100 is within 0.5 of 151.
	assert.equal(createdCanvases, 3, 'start, the ratio crossing at 151, and the settle at 100');
	console.log(
		`PERF-PAINT-10529 sweep: ${createdCanvases} tile builds for ${heights.length} frames (one per distinct height before)`
	);
});

test('a height move past the reuse ratio rebuilds at once at the new height', () => {
	resetCounts();
	sweep([100]);
	const calls = sweep([220]);
	assert.equal(createdCanvases, 2);
	assert.equal(tiles.at(-1).height, 220);
	assert.equal(calls.at(-1).length, 3, 'rebuilt tiles blit unscaled');
});

test('a height held for HEIGHT_SETTLE_FRAMES draws blits unscaled again', () => {
	assert.equal(render.BAND_CACHE_CFG.HEIGHT_SETTLE_FRAMES, 12);
	assert.equal(render.BAND_CACHE_CFG.HEIGHT_REUSE_RATIO, 0.5);
	resetCounts();
	sweep([100]);
	const calls = sweep(Array(render.BAND_CACHE_CFG.HEIGHT_SETTLE_FRAMES - 1).fill(130));
	assert.equal(createdCanvases, 1);
	const args = calls.at(-1);
	assert.equal(args.length, 9);
	assert.equal(args[4], 100, 'source height is the built tile height');
	assert.ok(Math.abs(args[6] + args[8] - 130) < 1e-9, 'row bottom lands on the row bottom');
	assert.ok(Math.abs(args[8] - 100 * (120 / 90)) < 1e-9);
	const settled = sweep([130]);
	assert.equal(createdCanvases, 2);
	assert.equal(settled.at(-1).length, 3);
	assert.equal(settled.at(-1)[0].height, 130, 'destination height equals the source tile height');
});

test('layered band blits map rows by the built height', () => {
	resetCounts();
	const extra = { bandGains: { low: 1, mid: 0.5, high: 1 } };
	sweep([100], extra);
	const calls = sweep([130], extra);
	assert.equal(createdCanvases, 3);
	assert.equal(calls.length, 3);
	for (const args of calls) {
		assert.equal(args.length, 9);
		assert.equal(args[4], 100);
	}
	assert.ok(
		Math.abs(calls[0][6] + calls[0][8] - 130) < 1e-9,
		'unity-gain low layer lands on the row bottom'
	);
});
