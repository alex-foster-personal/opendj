import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let render;

before(async () => {
	render = await loadTypeScriptModule('src/lib/components/rb/wave/render.ts');
});

function recordingCtx(width, height) {
	const fills = [];
	return {
		fills,
		ctx: {
			fillStyle: '#000',
			clearRect() {},
			fillRect(x, y, w, h) {
				fills.push({ x, y, w, h, fillStyle: this.fillStyle });
			}
		},
		width,
		height
	};
}

test('drawStemWaveRow paints non-empty bars from a fixture envelope', () => {
	const envelope = Array.from({ length: 100 }, (_, i) => (i === 50 ? 1 : 0));
	const rec = recordingCtx(100, 12);
	render.drawStemWaveRow(rec.ctx, {
		envelope,
		scrollPx: 50,
		durationMs: 100 * 240,
		pitch: 1,
		width: rec.width,
		height: rec.height,
		color: '#4fb2ff'
	});
	assert.ok(rec.fills.length > 0, 'fixture envelope must paint at least one bar');
	assert.ok(
		rec.fills.some((f) => f.fillStyle === '#4fb2ff'),
		'stem row must use the supplied stem colour'
	);
});

/**
 * The envelope spans the WHOLE track, so it must be read on the main row's
 * time scale (WAVE_WINDOW_S seconds x pitch across the canvas), not stretched
 * over one visible window (Codex P1 on #3645). A 180 s track, 1 point per
 * second, 240 px canvas: at pitch 1 that is 10 px per second of track.
 */
const WINDOW_S = 24;
const W = 240;

function spikeAt(points, index) {
	return Array.from({ length: points }, (_, i) => (i === index ? 1 : 0));
}

function paintedColumns({ positionS, spikeS, pitch = 1, durationS = 180 }) {
	const rec = recordingCtx(W, 12);
	const scrollPx = positionS * (W / (WINDOW_S * pitch));
	render.drawStemWaveRow(rec.ctx, {
		envelope: spikeAt(durationS, spikeS),
		scrollPx,
		durationMs: durationS * 1000,
		pitch,
		width: rec.width,
		height: rec.height,
		color: '#4fb2ff'
	});
	return rec.fills.map((f) => f.x);
}

test('stem row still paints the envelope under the playhead a minute into the track', () => {
	// The old read stretched all 180 points over one 240 px window, so every
	// index was out of range once the playhead passed ~36 s: nothing painted.
	assert.deepEqual(paintedColumns({ positionS: 60, spikeS: 60 }), [120, 121, 122, 123, 124, 125, 126, 127, 128, 129]);
});

test('stem row places a later peak at the main row px-per-second, not the window width', () => {
	// 10 s ahead of the playhead at 10 px/s lands 100 px right of center.
	assert.deepEqual(paintedColumns({ positionS: 60, spikeS: 70 }), [220, 221, 222, 223, 224, 225, 226, 227, 228, 229]);
	// Just outside the visible window (12 s + 1 s either side): nothing painted.
	assert.deepEqual(paintedColumns({ positionS: 60, spikeS: 73 }), []);
	assert.deepEqual(paintedColumns({ positionS: 60, spikeS: 47 }), []);
});

test('stem row widens its window with pitch exactly like the main row', () => {
	// pitch 2 shows 48 s of track: 5 px/s, so 10 s ahead is 50 px right of center.
	assert.deepEqual(paintedColumns({ positionS: 60, spikeS: 70, pitch: 2 }), [170, 171, 172, 173, 174]);
});

test('stem row paints nothing without a positive track duration or pitch', () => {
	// A flat full-scale envelope paints on ANY in-range index, so an invented
	// time scale (a negative duration or pitch flips the index sign; pitch 0
	// pins every column to point 0) would show up as fills.
	const cases = [
		{ durationMs: null, pitch: 1 },
		{ durationMs: 0, pitch: 1 },
		{ durationMs: -10_000, pitch: 1 },
		{ durationMs: Number.NaN, pitch: 1 },
		{ durationMs: 10_000, pitch: 0 },
		{ durationMs: 10_000, pitch: -1 }
	];
	for (const { durationMs, pitch } of cases) {
		const rec = recordingCtx(W, 12);
		render.drawStemWaveRow(rec.ctx, {
			envelope: Array.from({ length: 10 }, () => 1),
			scrollPx: 0,
			durationMs,
			pitch,
			width: rec.width,
			height: rec.height,
			color: '#4fb2ff'
		});
		assert.equal(rec.fills.length, 0, `durationMs=${durationMs} pitch=${pitch} must not invent a time scale`);
	}
	// Control: the same flat envelope on a real 10 s track at pitch 1 does paint.
	const rec = recordingCtx(W, 12);
	render.drawStemWaveRow(rec.ctx, {
		envelope: Array.from({ length: 10 }, () => 1),
		scrollPx: 0,
		durationMs: 10_000,
		pitch: 1,
		width: rec.width,
		height: rec.height,
		color: '#4fb2ff'
	});
	assert.equal(rec.fills.length, 100, 'track seconds 0..10 sit right of center at 10 px/s');
});
