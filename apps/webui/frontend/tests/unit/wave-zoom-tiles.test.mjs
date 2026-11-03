// requirement: WAVEZOOM-01, WAVEZOOM-02, WAVEZOOM-6802b
// [if] a long track's band cache allocates one canvas wider than TILE_PX [then] this fails, [else stop].
// [if] a frame builds tiles it cannot see [then] this fails, [else stop].
// [if] the tiles, laid side by side, paint different bars than one whole-track image (tri-band or blocks) [then] this fails, [else stop].
// [if] the line design loses a segment at a tile edge [then] this fails, [else stop].
// [if] the beat grid draws lines closer than BEAT_LINE_MIN_GAP_PX when zoomed out [then] this fails, [else stop].
// [if] the thinning also drops beats at today's 24 s window [then] this fails, [else stop].
// [if] a zoom glide rebuilds the band cache on its frames instead of scaling one cache [then] this fails, [else stop].
// [if] a faster tempo later in the window draws lines closer than the gap [then] this fails, [else stop].
// Tile ARITHMETIC only: the recording canvas below cannot see real pixels, so
// the real-browser proof that tiles paint seamlessly is
// tests/e2e/wave-band-tiles.spec.ts (Codex P1 on PR #6786).
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let render;
let math;
/** Every canvas the band cache creates, in creation order. */
let canvases = [];

class RecordingPath {
	constructor() {
		this.rects = [];
		this.ops = [];
	}
	rect(x, y, w, h) {
		this.rects.push([x, y, w, h]);
	}
	moveTo(x, y) {
		this.ops.push(['M', x, y]);
	}
	lineTo(x, y) {
		this.ops.push(['L', x, y]);
	}
}

function recordingContext() {
	const fills = [];
	const draws = [];
	return {
		fills,
		draws,
		fillStyle: '',
		strokeStyle: '',
		lineWidth: 1,
		globalAlpha: 1,
		// Blocks bars paint with fillRect (PGWAVE-7692b); bands still fill a path.
		fillRect(x, y, w, h) {
			fills.push([this.fillStyle, x, y, w, h]);
		},
		fill(path) {
			for (const r of path.rects) fills.push([this.fillStyle, ...r]);
		},
		drawImage(...args) {
			draws.push(args);
		},
		strokes: [],
		beginPath() {},
		moveTo() {},
		lineTo() {},
		closePath() {},
		stroke(path) {
			if (path !== undefined) this.strokes.push(path.ops);
		}
	};
}

before(async () => {
	globalThis.Path2D = RecordingPath;
	globalThis.document = {
		createElement(tag) {
			assert.equal(tag, 'canvas');
			const ctx = recordingContext();
			const canvas = { width: 0, height: 0, ctx, getContext: () => ctx };
			canvases.push(canvas);
			return canvas;
		}
	};
	render = await loadTypeScriptModule('src/lib/components/rb/wave/render.ts');
	math = await loadTypeScriptModule('src/lib/components/rb/wave/wave-math.ts');
});

const PALETTE = {
	bg: '#101010', low: '#e8a13a', mid: '#4f8fff', high: '#f0f0f0', mono: '#bbbbbb', tick: '#ccc',
	cueHotCue: '#35c04f', cueLoop: '#e8a13a', cueMemory: '#ff3b30', cueOutline: '#c8cdd2', phrase: '#888'
};

// 1000 s of varied band data: 10,000 px at the 24 s window on a 240 px row,
// so three tiles (4095 + 4095 + 1810).
const N = 3000;
const wave = (f) => Array.from({ length: N }, (_, i) => 0.15 + 0.8 * Math.abs(Math.sin(i * f)));
const detail = { length: N, low: wave(0.031), mid: wave(0.017), high: wave(0.053) };
const anlz = {
	stable_id: 'b'.repeat(40), points: N, waveform: { kind: 'tri', preview: detail, detail },
	beatgrid: { beat_count: 0, beats: [] }, cues: [], phrases: [], vocals: { status: 'not_analyzed' }
};
const DUR_MS = 1_000_000;
const ROW_W = 240;
const ROW_H = 60;
const PX_PER_S = ROW_W / 24;

function frame(positionMs, design) {
	return {
		windowS: 24,
		playheadFrac: 0.5,
		widthCss: ROW_W, heightCss: ROW_H, positionMs, durationMs: DUR_MS, anlz,
		palette: PALETTE, pitch: 1, loop: null, waveformDesign: design
	};
}

test('a long track is cached in tiles no wider than TILE_PX, built only where visible', () => {
	render.resetWaveBandCacheForTest();
	canvases = [];
	const ctx = recordingContext();
	render.drawWaveRow(ctx, frame(30_000, 'tri-band'));
	assert.equal(canvases.length, 1, 'only the tile under the window is built');
	assert.equal(canvases[0].width, render.BAND_CACHE_CFG.TILE_PX);
	render.drawWaveRow(ctx, frame(900_000, 'tri-band'));
	assert.equal(canvases.length, 2, 'scrolling far builds the tile it reaches');
	assert.equal(canvases[1].width, 10_000 - 2 * render.BAND_CACHE_CFG.TILE_PX, 'the last tile is cut to the track');
	assert.ok(canvases.every((c) => c.width <= render.BAND_CACHE_CFG.TILE_PX));
});

test('a window straddling a tile edge blits both tiles at their track positions', () => {
	render.resetWaveBandCacheForTest();
	canvases = [];
	const ctx = recordingContext();
	const edgeS = render.BAND_CACHE_CFG.TILE_PX / PX_PER_S;
	render.drawWaveRow(ctx, frame(edgeS * 1000, 'tri-band'));
	assert.equal(ctx.draws.length, 2);
	const tLeft = edgeS - 12;
	assert.deepEqual(
		ctx.draws.map((d) => d[1]),
		[0 - tLeft * PX_PER_S, render.BAND_CACHE_CFG.TILE_PX - tLeft * PX_PER_S],
		'each tile lands at its own x on the row'
	);
});

test('a window ending exactly on a tile edge builds no zero-width tile past it', () => {
	// Sol P1 on PR #6786: the window's last visible px sits on the edge where the
	// next tile starts, so that tile has no visible source and must not be built.
	render.resetWaveBandCacheForTest();
	canvases = [];
	const ctx = recordingContext();
	const edgeS = render.BAND_CACHE_CFG.TILE_PX / PX_PER_S;
	const tLeft = edgeS - ROW_W / PX_PER_S;
	render.drawWaveRow(ctx, frame((tLeft + ROW_W / PX_PER_S / 2) * 1000, 'tri-band'));
	assert.equal(canvases.length, 1, 'only the tile holding the visible span is built');
	assert.equal(ctx.draws.length, 1);
});

for (const design of ['tri-band', 'blocks']) {
	test(`${design}: tiles side by side paint exactly the whole-track image`, () => {
		render.resetWaveBandCacheForTest();
		canvases = [];
		const ctx = recordingContext();
		for (let s = 0; s <= 1000; s += 20) render.drawWaveRow(ctx, frame(s * 1000, design));
		assert.equal(canvases.length, 3);
		const tiled = canvases.flatMap((c, k) =>
			c.ctx.fills.map(([style, x, y, w, h]) => [style, x + k * render.BAND_CACHE_CFG.TILE_PX, y, w, h])
		);
		const whole = recordingContext();
		render.drawWaveBands(whole, anlz.waveform, 10_000, ROW_H, PALETTE, design);
		const key = (r) => r.join(',');
		assert.ok(whole.fills.length > 1000, 'the reference paints real bars');
		assert.deepEqual(tiled.map(key).sort(), whole.fills.map(key).sort());
	});
}

test('a zoom glide scales one cache built at the wider step; settling rebuilds once', () => {
	render.resetWaveBandCacheForTest();
	canvases = [];
	const ctx = recordingContext();
	const glide = (windowS, cacheWindowS) =>
		render.drawWaveRow(ctx, { ...frame(500_000, 'tri-band'), windowS, cacheWindowS });
	for (const windowS of [24, 30, 38, 46, 48]) glide(windowS, 48);
	const built = canvases.length;
	assert.ok(built >= 1 && built <= 2, `the glide built ${built} tiles, all at the 48 s scale`);
	assert.ok(canvases.every((c) => c.width <= render.BAND_CACHE_CFG.TILE_PX));
	render.drawWaveRow(ctx, { ...frame(500_000, 'tri-band'), windowS: 48 });
	assert.equal(canvases.length, built, 'settling on the wider step reuses the glide cache');
	for (const windowS of [40, 30, 24]) render.drawWaveRow(ctx, { ...frame(500_000, 'tri-band'), windowS });
	assert.ok(canvases.length > built + 1, 'control: without cacheWindowS each new scale rebuilds');
});

/** Every stroked segment, as "x0,y0>x1,y1" keys, shifted right by dx. */
function segments(opLists, dx = 0) {
	const out = new Set();
	const k = (v) => v.toFixed(4);
	for (const ops of opLists) {
		let prev = null;
		for (const [op, x, y] of ops) {
			if (op === 'L' && prev !== null) out.add(`${k(prev[0] + dx)},${k(prev[1])}>${k(x + dx)},${k(y)}`);
			prev = [x, y];
		}
	}
	return out;
}

test('line: tiles stroke every segment of the whole-track line, across each tile edge too', () => {
	render.resetWaveBandCacheForTest();
	canvases = [];
	const ctx = recordingContext();
	for (let s = 0; s <= 1000; s += 20) render.drawWaveRow(ctx, frame(s * 1000, 'line'));
	assert.equal(canvases.length, 3);
	const tiled = new Set();
	canvases.forEach((c, k) => {
		for (const seg of segments(c.ctx.strokes, k * render.BAND_CACHE_CFG.TILE_PX)) tiled.add(seg);
	});
	const whole = recordingContext();
	render.drawWaveBands(whole, anlz.waveform, 10_000, ROW_H, PALETTE, 'line');
	const want = segments(whole.strokes);
	assert.ok(want.size > 10_000, 'the reference strokes a real line');
	const missing = [...want].filter((seg) => !tiled.has(seg));
	assert.deepEqual(missing, [], 'no whole-track segment is lost at a tile edge');
	const extra = [...tiled].filter((seg) => !want.has(seg));
	assert.deepEqual(extra, [], 'tiles stroke nothing the whole track does not');
	// Each tile clips its stroke at its own canvas edge, so the segment that
	// crosses an edge must be stroked by BOTH tiles or half of it goes missing.
	const tile = render.BAND_CACHE_CFG.TILE_PX;
	for (let k = 1; k < canvases.length; k++) {
		const edge = k * tile;
		const crossing = [...want].filter((seg) => {
			const [a, b] = seg.split('>').map((p) => Number(p.split(',')[0]));
			return a < edge && b >= edge;
		});
		assert.ok(crossing.length > 0, `a segment crosses edge ${k}`);
		const left = segments(canvases[k - 1].ctx.strokes, (k - 1) * tile);
		const right = segments(canvases[k].ctx.strokes, k * tile);
		for (const seg of crossing) {
			assert.ok(left.has(seg), `tile ${k - 1} strokes ${seg}`);
			assert.ok(right.has(seg), `tile ${k} strokes ${seg}`);
		}
	}
});

// 128 BPM, 4/4, 5 minutes.
const BEATS = Array.from({ length: 640 }, (_, i) => ({ n: (i % 4) + 1, bpm: 128, t: 0.1 + (i * 60) / 128 }));

test('the grid keeps every beat at the default 24 s window', () => {
	const pxPerS = 1120 / 24;
	const lines = math.visibleBeatLines(BEATS, 100, pxPerS, 1120, 56);
	assert.equal(lines.length, 51, 'about 51 beats fit in 24 s at 128 BPM');
	assert.ok(lines.some((l) => !l.isDownbeat));
});

test('zoomed out to the whole track the grid thins to bar groups, never closer than the gap', () => {
	const pxPerS = 1120 / 300;
	const lines = math.visibleBeatLines(BEATS, 0, pxPerS, 1120, 56);
	assert.ok(lines.length > 0 && lines.length < 60, `drew ${lines.length} lines`);
	assert.ok(lines.every((l) => l.isDownbeat), 'only bar lines survive');
	for (let i = 1; i < lines.length; i++) {
		assert.ok(lines[i].x - lines[i - 1].x >= math.BEAT_LINE_MIN_GAP_PX - 1, 'lines keep the minimum gap');
	}
});

test('thinned bar lines stay on the same bars as the window scrolls', () => {
	const pxPerS = 1120 / 300;
	const barOf = (t) => Math.round(((t - 0.1) * 128) / 60 / 4);
	const at = (tLeft) =>
		math.visibleBeatLines(BEATS, tLeft, pxPerS, 600, 56).map((l) => barOf(tLeft + l.x / pxPerS));
	const bars = [...at(0), ...at(37.3)];
	const stride = math.beatLineStride((60 / 128) * pxPerS) / 4;
	assert.ok(stride >= 1);
	assert.ok(bars.every((b) => b % stride === 0), `every drawn bar is a multiple of ${stride}`);
});

test('a faster tempo later in the window still keeps the minimum gap (stride from the closest beats)', () => {
	// 100 BPM for 64 beats, then 150 BPM: at 5 px/s one line per bar is 12 px
	// apart in the first section but only 8 px in the second.
	const beats = [];
	let t = 0.1;
	for (let i = 0; i < 256; i++) {
		beats.push({ n: (i % 4) + 1, bpm: i < 64 ? 100 : 150, t });
		t += i < 64 ? 0.6 : 0.4;
	}
	const pxPerS = 5;
	const lines = math.visibleBeatLines(beats, 0, pxPerS, 600, 56);
	assert.ok(lines.length > 3, `drew ${lines.length} lines`);
	for (let i = 1; i < lines.length; i++) {
		assert.ok(lines[i].x - lines[i - 1].x >= math.BEAT_LINE_MIN_GAP_PX - 1, `gap ${lines[i].x - lines[i - 1].x} px`);
	}
	// Control: the slow section alone still keeps one line per bar.
	const slow = math.visibleBeatLines(beats.slice(0, 64), 0, pxPerS, 600, 56);
	assert.equal(slow.length, 16);
});

test('without a trusted bar phase the grid thins by beat count', () => {
	const pxPerS = 1120 / 300;
	const lines = math.visibleBeatLines(BEATS, 0, pxPerS, 1120, 56, true);
	assert.ok(lines.length > 0 && lines.every((l) => !l.isDownbeat));
	for (let i = 1; i < lines.length; i++) assert.ok(lines[i].x - lines[i - 1].x >= math.BEAT_LINE_MIN_GAP_PX - 1);
});

test('a trusted bar and an untrusted beat one beat apart never both draw inside the gap', () => {
	// Sol P1 on PR #6786: beat 7 is a trusted downbeat and beat 8 is untrusted at
	// an index the untrusted stride keeps. Both pass their own rule, 4 px apart.
	const beats = Array.from({ length: 16 }, (_, i) => ({
		n: i === 7 ? 1 : (i % 4) + 2,
		bpm: 120,
		t: i * 0.5,
		bar_phase_untrusted: i === 8
	}));
	const lines = math.visibleBeatLines(beats, 0, 8, 200, 56);
	assert.ok(lines.length > 0, 'draws the grid');
	for (let i = 1; i < lines.length; i++) {
		assert.ok(lines[i].x - lines[i - 1].x >= math.BEAT_LINE_MIN_GAP_PX, `gap ${lines[i].x - lines[i - 1].x} px`);
	}
});
