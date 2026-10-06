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
