import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// PERF-PAINT-01 regression lines:
// - if a sub-pixel playhead advance rebuilds the band image then broken
// - if a one-pixel advance does not move the cached band image then broken
// - if width or zoom changes reuse the old band image then broken

let render;
let createdCanvases = 0;

class RecordingPath {
	constructor() {
		this.rects = [];
	}
	rect(x, y, w, h) {
		this.rects.push({ x, y, w, h });
	}
}

function context(calls) {
	return {
		fillStyle: '',
		strokeStyle: '',
		lineWidth: 1,
		globalAlpha: 1,
		fillRect() {},
		fill() {},
		drawImage(...args) {
			calls.push(args);
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
			return { width: 0, height: 0, getContext: () => context(calls) };
		}
	};
	render = await loadTypeScriptModule('src/lib/components/rb/wave/render.ts');
});

const PALETTE = {
	bg: '#101010', low: '#e8a13a', mid: '#4f8fff', high: '#f0f0f0', tick: '#ccc',
	cueHotCue: '#35c04f', cueLoop: '#e8a13a', cueMemory: '#ff3b30', cueOutline: '#c8cdd2', phrase: '#888'
};

const detail = { length: 1200, low: Array(1200).fill(0.5), mid: Array(1200).fill(0.5), high: Array(1200).fill(0.5) };
const anlz = {
	stable_id: 'a'.repeat(40), points: 1200, waveform: { kind: 'tri', preview: detail, detail },
	beatgrid: { beat_count: 0, beats: [] }, cues: [], phrases: [], vocals: { status: 'not_analyzed' }
};

function frame(positionMs, widthCss = 240, pitch = 1) {
	return { widthCss, heightCss: 60, positionMs, durationMs: 120_000, anlz, palette: PALETTE, pitch, loop: null };
}

test('sub-pixel advances blit the cached band image without rebuilding its buckets', () => {
	render.resetWaveBandCacheForTest();
	createdCanvases = 0;
	const calls = [];
	const ctx = context(calls);
	render.drawWaveRow(ctx, frame(30_000));
	const firstBuilds = createdCanvases;
	render.drawWaveRow(ctx, frame(30_050)); // 0.5 CSS px at 10px/s
	assert.equal(createdCanvases, firstBuilds, 'a move below one CSS pixel must not rescan or rebuild bands');
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
	assert.ok(Math.abs(destX - 120) < 1e-6, `playhead maps to x=${destX}, expected 120`);
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
const GRID_BEATS = Array.from({ length: 200 }, (_, i) => ({ n: (i % 4) + 1, bpm: 120, t: 0.513 + i * 0.5 }));
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

test('a snapped Gothic row keeps every beat edge on its grid line while scrolling', async () => {
	const layout = await loadTypeScriptModule('src/lib/components/rb/wave/block-layout.ts');
	const pxPerS = GRID_W / render.WAVE_WINDOW_S;
	let fractionalSeen = 0;
	for (let ms = 30_000; ms < 30_400; ms += 7) {
		render.resetWaveBandCacheForTest();
		const f = gridFrame(ms, 'blocks');
		const { ctx, blits, lines } = recordingContext();
		render.drawWaveRow(ctx, f);
		const dx = blits.at(-1)[1];
		assert.ok(Number.isInteger(dx), `blit offset ${dx} at ${ms} ms is a whole pixel`);
		const tLeft = ms / 1000 - render.WAVE_WINDOW_S / 2;
		if (!Number.isInteger(tLeft * pxPerS)) fractionalSeen += 1;
		const visible = f.anlz.beatgrid.beats.filter((b) => b.t >= tLeft && b.t <= tLeft + render.WAVE_WINDOW_S);
		const edges = Array.from(layout.beatAnchorsPx(visible, pxPerS), (x) => x + dx);
		assert.ok(edges.length > 0, 'CONTROL: the window holds beats');
		assert.deepEqual(lines, edges, `beat lines sit on their block edges at ${ms} ms`);
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
test('a snapped row under a Beat Sync nudge keeps each beat edge within 1.52 px of its line', async () => {
	const layout = await loadTypeScriptModule('src/lib/components/rb/wave/block-layout.ts');
	const bound = 1 + 0.5 * (1 + render.BAND_CACHE_CFG.PITCH_TOLERANCE);
	assert.ok(bound < 1.52);
	let worst = 0;
	for (const pitch of [0.975, 0.99, 1.01, 1.025]) {
		for (let ms = 30_000; ms < 30_400; ms += 13) {
			render.resetWaveBandCacheForTest();
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
			const visible = f.anlz.beatgrid.beats.filter((b) => b.t >= tLeft && b.t <= tLeft + trackWindowS);
			const edges = Array.from(layout.beatAnchorsPx(visible, builtPxPerS), (x) => dx + ((x - sx) * dw) / sw);
			assert.equal(lines.length, edges.length, 'CONTROL: one line per visible beat');
			for (let i = 0; i < edges.length; i++) worst = Math.max(worst, Math.abs(lines[i] - edges[i]));
		}
	}
	assert.ok(worst > 0.5, `CONTROL: the sweep reaches the rounding error (worst ${worst})`);
	assert.ok(worst < bound, `worst ${worst.toFixed(3)} px against ${bound.toFixed(3)} px`);
});
