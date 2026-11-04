import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// Gothic 'blocks' rows snap block edges to changes (the maintainer, Tue 13 Oct 2026:
// blocks "could lose the 'edge' of where the beat/downbeat is from averaging").
// Regression lines:
// - if a moderate energy change does not start a block (its LEFT edge) then broken
// - if the block before a change reaches the change's detail point then broken
// - if a painted beat with no change nearby does not start a block then broken
// - if a beat within BEAT_YIELD_PX of a change wins over the change then broken
// - if a block pitch leaves BLOCK_PITCH_MIN_PX..BLOCK_PITCH_MAX_PX or the 1px gap changes then broken
// - if a stretch with no change and no beat stops painting the fixed 3px pitch then broken (overshoot control)
// - if blocksSnap false stops painting exactly the fixed-pitch row then broken
// - if the production row stops snapping by default then broken
// - if a snapped bar straddling a band-cache tile edge loses columns in either tile then broken

const ROW_W = 1200;
const ROW_H = 60;

let render;
let layout;
const windows = [];

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
	layout = await loadTypeScriptModule('src/lib/components/rb/wave/block-layout.ts');
	// The snapper is a lazy chunk in the app (off the "/" first paint); load it
	// up front so every row below paints the production default.
	await render.__test_loadBlockSnap();
	// Real captures (provenance in each manifest). Window start and span from
	// the manifests: the clip window is points 3072..6144 of 38400 over 305 s,
	// the dense-mix window points 14419..18023 of 32292 over 215 s.
	for (const [file, t0, durS] of [
		['gothic-blocks-clip-window.json', (3072 * 305) / 38400, (3072 * 305) / 38400],
		['blocks-variance-anlz-window.json', (14419 * 215) / 32292, (3604 * 215) / 32292]
	]) {
		const fx = JSON.parse(readFileSync(new URL(`./fixtures/${file}`, import.meta.url), 'utf8'));
		const d = fx.detail;
		const detail = { low: d.low, mid: d.mid, high: d.high, length: d.low.length };
		// Beat times relative to the window, as a whole-track image would hold them.
		const beats = fx.beats.map((b) => ({ n: b.n ?? 1, bpm: b.bpm ?? 120, t: b.t - t0 }));
		windows.push({ file, durS, detail, beats });
	}
});

function anlzOf(detail, beats) {
	return {
		stable_id: 'a'.repeat(40),
		points: detail.length,
		waveform: { kind: 'mono', preview: detail, detail },
		beatgrid: { beat_count: beats.length, beats },
		cues: [],
		phrases: [],
		vocals: { status: 'not_analyzed' },
		beatgrid_source: 'rekordbox',
		beatgrid_own_unavailable_reason: null
	};
}

/** Bars the production drawWaveRow paints across one whole window, left to right. */
function rowBars(win, frame = {}) {
	const mono = '#cfcfcf';
	const palette = {
		bg: '#000000', low: '#111111', mid: '#222222', high: '#333333', mono, tick: '#444444',
		cueHotCue: '#35c04f', cueLoop: '#e8a13a', cueMemory: '#ff3b30', cueOutline: '#c8cdd2', phrase: '#888888'
	};
	const rects = [];
	const base = {
		fillStyle: '',
		// Block bars paint with fillRect (WebKit antialiases a Path2D fill of 2px bars); other marks use paths.
		fillRect(x, y, w, h) {
			if (this.fillStyle === mono) rects.push({ x, y, w, h });
		},
		fill(path) {
			if (this.fillStyle === mono) rects.push(...path.rects);
		}
	};
	const ctx = new Proxy(base, { get: (t, k) => (k in t ? t[k] : () => {}), set: (t, k, v) => ((t[k] = v), true) });
	const durationMs = win.durS * 1000;
	render.drawWaveRow(ctx, {
		widthCss: ROW_W, heightCss: ROW_H, positionMs: durationMs / 2, durationMs, anlz: anlzOf(win.detail, win.beats),
		palette, pitch: win.durS / render.WAVE_WINDOW_S, windowS: render.WAVE_WINDOW_S, playheadFrac: 0.5, loop: null, waveformDesign: 'blocks', ...frame
	});
	return rects.sort((a, b) => a.x - b.x);
}

/** The 'true-peak' row's norm: the loudest point of any band, the painter's monoNorm. */
function peakNorm(detail) {
	let peak = 0;
	for (const band of [detail.low, detail.mid, detail.high]) for (const v of band) if (v > peak) peak = v;
	return { divisor: peak, clampAtOne: false };
}
const UNIT = { divisor: 1, clampAtOne: false };

/** The layout the row builds, from the same pure pieces. */
function edgesOf(win, beats = win.beats) {
	const pxPerS = ROW_W / win.durS;
	const changes = layout.changeAnchorsPx(layout.detectChangePoints(win.detail, win.durS, peakNorm(win.detail)), win.detail.length, ROW_W);
	return { changes, edges: layout.snappedBlockEdges(ROW_W, changes, layout.beatAnchorsPx(beats, pxPerS)) };
}

test('the fixtures carry moderate changes that a fixed 3px pitch splits mid-block (control)', () => {
	for (const win of windows) {
		const { changes } = edgesOf(win);
		assert.ok(changes.length >= 5, `${win.file}: ${changes.length} changes`);
		const midBlock = Array.from(changes).filter((x) => x % render.BLOCK_PITCH_PX !== 0);
		assert.ok(midBlock.length >= 3, `${win.file}: only ${midBlock.length} changes fall mid-block on the fixed pitch`);
	}
});

test('every moderate change starts a block, and the block before it holds none of the change', () => {
	for (const win of windows) {
		const { changes, edges } = edgesOf(win);
		const starts = new Set(edges);
		const n = win.detail.length;
		const points = layout.detectChangePoints(win.detail, win.durS, peakNorm(win.detail));
		for (let c = 0; c < changes.length; c++) {
			const x = changes[c];
			if (c > 0 && x - changes[c - 1] < layout.BLOCK_SNAP_CFG.MIN_ANCHOR_GAP_PX) continue; // merged
			assert.ok(starts.has(x), `${win.file}: change at ${x}px starts no block`);
			const b = edges.indexOf(x);
			// The painter's bucket for the block before: points up to ceil(x*n/w)-1.
			const prevLast = Math.ceil((edges[b] / ROW_W) * n) - 1;
			assert.ok(prevLast < points[c], `${win.file}: block before ${x}px reaches change point ${points[c]}`);
			const ownFirst = Math.floor((edges[b] / ROW_W) * n);
			assert.ok(ownFirst <= points[c], `${win.file}: block at ${x}px starts after its change`);
		}
	}
});

test('a painted beat with no change nearby starts a block; one near a change yields to it', () => {
	for (const win of windows) {
		const { changes, edges } = edgesOf(win);
		const starts = new Set(edges);
		const beatX = layout.beatAnchorsPx(win.beats, ROW_W / win.durS);
		let onGrid = 0;
		for (const x of beatX) {
			if (x < 0 || x >= ROW_W) continue; // off the row
			const near = Array.from(changes).some((c) => Math.abs(c - x) < layout.BLOCK_SNAP_CFG.BEAT_YIELD_PX);
			if (near) continue;
			assert.ok(starts.has(x), `${win.file}: beat at ${x}px starts no block`);
			onGrid++;
		}
		assert.ok(onGrid >= 30, `${win.file}: only ${onGrid} beats checked`);
	}
	// Yield: a beat 2px after a change is dropped, the change kept.
	const edges = layout.snappedBlockEdges(60, [30], [32]);
	assert.ok(edges.includes(30) && !edges.includes(32), Array.from(edges).join(','));
	// A beat 3px away stays.
	assert.ok(layout.snappedBlockEdges(60, [30], [33]).includes(33));
});

test('pitch stays within 2..4 px and the painted gap stays 1px', () => {
	assert.equal(layout.BLOCK_PITCH_MIN_PX, 2);
	assert.equal(layout.BLOCK_PITCH_MAX_PX, 4);
	assert.equal(render.BLOCK_GAP_PX, 1);
	for (const win of windows) {
		const { edges } = edgesOf(win);
		assert.ok(edges[0] === 0 || edges[0] === 1, `${win.file}: first edge ${edges[0]}`);
		assert.ok(edges[edges.length - 1] >= ROW_W && edges[edges.length - 1] < ROW_W + 3, `${win.file}: closing edge ${edges[edges.length - 1]}`);
		for (let b = 1; b < edges.length; b++) {
			const p = edges[b] - edges[b - 1];
			assert.ok(p >= layout.BLOCK_PITCH_MIN_PX && p <= layout.BLOCK_PITCH_MAX_PX, `${win.file}: pitch ${p} at ${edges[b - 1]}`);
		}
		const bars = rowBars(win);
		assert.ok(bars.length > 100, `${win.file}: ${bars.length} bars`);
		for (let i = 1; i < bars.length; i++) {
			assert.ok(bars[i].x - (bars[i - 1].x + bars[i - 1].w) >= render.BLOCK_GAP_PX, `${win.file}: bars touch at ${bars[i].x}`);
		}
		for (const r of bars) assert.ok(r.w >= 1 && r.w <= 3, `${win.file}: bar ${r.w}px at ${r.x}`);
	}
});

test('with no change and no beat the layout is the fixed 3px pitch (overshoot control)', () => {
	// Any width, not just multiples of 3: no anchor means the old grid exactly.
	for (let w = 1; w <= 61; w++) {
		assert.deepEqual(Array.from(layout.snappedBlockEdges(w, [], [])), Array.from(render.uniformBlockEdges(w)), `width ${w}`);
	}
	// After the last anchor the tail is the fixed pitch from that anchor, not stretched to w.
	assert.deepEqual(Array.from(layout.snappedBlockEdges(20, [6], [])), [0, 3, 6, 9, 12, 15, 18, 21]);
	// A flat signal finds no change at all.
	const flat = Array.from({ length: 1000 }, () => 0.5);
	assert.equal(layout.detectChangePoints({ low: flat, mid: flat, high: flat, length: 1000 }, 8, UNIT).length, 0);
	// A single step finds exactly one, at the step.
	const step = Array.from({ length: 1000 }, (_, i) => (i < 400 ? 0.1 : 0.9));
	assert.deepEqual(Array.from(layout.detectChangePoints({ low: step, mid: step, high: step, length: 1000 }, 8, UNIT)), [400]);
});

test('change detection keeps at least REFRACTORY_S between changes at any point rate', () => {
	// 126 points/s: 0.08 s is 10.08 points, so two rises 10 points (79.4 ms) apart
	// must yield one change. Rounding the refractory to 10 points would emit both.
	const n = 1260;
	const low = Array.from({ length: n }, (_, i) => (i === 400 || i === 410 ? 1 : 0.05));
	const quiet = Array.from({ length: n }, () => 0.05);
	const detail = { low, mid: quiet, high: quiet, length: n };
	assert.deepEqual(Array.from(layout.detectChangePoints(detail, 10, UNIT)), [400]);
	// Control: 11 points (87.3 ms) apart is past the refractory, so both count.
	const low2 = Array.from({ length: n }, (_, i) => (i === 400 || i === 411 ? 1 : 0.05));
	assert.deepEqual(Array.from(layout.detectChangePoints({ ...detail, low: low2 }, 10, UNIT)), [400, 411]);
});

test('two changes under 2 px apart share one block that starts at the earlier', () => {
	const edges = Array.from(layout.snappedBlockEdges(60, [30, 31], []));
	assert.ok(edges.includes(30) && !edges.includes(31), edges.join(','));
	for (let b = 1; b < edges.length; b++) assert.ok(edges[b] - edges[b - 1] >= layout.BLOCK_PITCH_MIN_PX, edges.join(','));
	// Control: 2 px apart both start a block.
	const two = Array.from(layout.snappedBlockEdges(60, [30, 32], []));
	assert.ok(two.includes(30) && two.includes(32), two.join(','));
});

test('change detection reads the level the painter shows, not per-band levels', () => {
	const n = 1000;
	const quiet = Array.from({ length: n }, () => 0.02);
	// The high band steps 0.02 -> 0.1 while the low band holds 0.9: per-band
	// normalization calls that a full-height rise, the painted max band shows none.
	const high = Array.from({ length: n }, (_, i) => (i < 400 ? 0.02 : 0.1));
	const low = Array.from({ length: n }, () => 0.9);
	const detail = { low, mid: quiet, high, length: n };
	assert.equal(layout.detectChangePoints(detail, 8, { divisor: 0.9, clampAtOne: false }).length, 0);
	// Control: the same step in the painted max band is a change.
	const loud = Array.from({ length: n }, (_, i) => (i < 400 ? 0.1 : 0.9));
	assert.deepEqual(Array.from(layout.detectChangePoints({ low: loud, mid: quiet, high: quiet, length: n }, 8, { divisor: 0.9, clampAtOne: false })), [400]);
	// legacy-p99 clamps at full height like its painter: a rise that only
	// happens above the divisor paints nothing new, so it is no change.
	const over = Array.from({ length: n }, (_, i) => (i < 400 ? 0.6 : 1));
	const od = { low: over, mid: quiet, high: quiet, length: n };
	assert.equal(layout.detectChangePoints(od, 8, { divisor: 0.5, clampAtOne: true }).length, 0);
	assert.deepEqual(Array.from(layout.detectChangePoints(od, 8, { divisor: 0.5, clampAtOne: false })), [400], 'control: unclamped it rises');
});

test('production rows snap by default; blocksSnap false paints exactly the fixed-pitch row', () => {
	assert.equal(render.BLOCKS_CFG.SNAP_DEFAULT, true);
	for (const win of windows) {
		const fixed = rowBars(win, { blocksSnap: false });
		for (const r of fixed) {
			assert.equal(r.x % render.BLOCK_PITCH_PX, 0);
			assert.equal(r.w, render.BLOCK_BAR_PX);
		}
		const snapped = rowBars(win);
		const starts = new Set(snapped.map((r) => r.x));
		const { changes } = edgesOf(win);
		const lit = Array.from(changes).filter((x) => x % render.BLOCK_PITCH_PX !== 0 && starts.has(x));
		assert.ok(lit.length >= 3, `${win.file}: default row put only ${lit.length} off-grid changes on a bar edge`);
	}
});

test('the block at a change steps up from the block before it more than on the fixed pitch', () => {
	for (const win of windows) {
		const { changes, edges } = edgesOf(win);
		const peaks = render.bandPeaksFor({ detail: win.detail });
		const snapH = render.blockHeights(win.detail, ROW_W, peaks, 'max', null, 'true-peak', edges);
		const fixedH = render.blockHeights(win.detail, ROW_W, peaks, 'max', null, 'true-peak');
		let snapStep = 0;
		let fixedStep = 0;
		for (const x of changes) {
			const b = edges.indexOf(x);
			if (b < 1) continue;
			snapStep += snapH[b] - snapH[b - 1];
			const f = Math.floor(x / render.BLOCK_PITCH_PX);
			// On the fixed pitch the change lights the block it falls in.
			if (f >= 1) fixedStep += fixedH[f] - fixedH[f - 1];
		}
		assert.ok(snapStep > fixedStep, `${win.file}: snapped step ${snapStep.toFixed(2)} vs fixed ${fixedStep.toFixed(2)}`);
	}
});

test('before the lazy snapper lands the cached band image holds the fixed pitch, then it is rebuilt snapped', async () => {
	// The browser path: with a document, drawWaveRow builds the whole-track band
	// image once and blits it per frame. Record what each built image paints.
	const mono = '#cfcfcf';
	const images = [];
	const recordingCtx = (rects) => {
		const base = {
			fillStyle: '',
			fillRect(x, y, w, h) { if (this.fillStyle === mono) rects.push({ x, y, w, h }); },
			fill(path) { if (this.fillStyle === mono) rects.push(...path.rects); }
		};
		return new Proxy(base, { get: (t, k) => (k in t ? t[k] : () => {}), set: (t, k, v) => ((t[k] = v), true) });
	};
	const hadDocument = 'document' in globalThis;
	const prevDocument = globalThis.document;
	globalThis.document = {
		createElement(tag) {
			assert.equal(tag, 'canvas');
			const rects = [];
			images.push(rects);
			const ctx = recordingCtx(rects);
			return { width: 0, height: 0, getContext: () => ctx };
		}
	};
	try {
		// A fresh module instance: nothing loaded yet, empty band image cache.
		const fresh = await loadTypeScriptModule('src/lib/components/rb/wave/render.ts');
		const win = windows[0];
		// One analysis object across frames, as a loaded deck holds it: the band
		// image is cached per waveform object.
		const anlz = anlzOf(win.detail, win.beats);
		const paint = () => {
			const prev = render;
			render = fresh;
			try {
				rowBars(win, { anlz });
			} finally {
				render = prev;
			}
		};
		paint();
		assert.equal(images.length, 1, 'the first frame builds one band image');
		assert.ok(images[0].length > 100, `the band image paints the row's bars (${images[0].length})`);
		for (const r of images[0]) assert.equal(r.x % fresh.BLOCK_PITCH_PX, 0, 'unloaded: fixed pitch');
		paint();
		assert.equal(images.length, 1, 'while unloaded the next frame blits the same image');
		await fresh.__test_loadBlockSnap();
		paint();
		assert.equal(images.length, 2, 'the snapper landing rebuilds the band image');
		assert.ok(images[1].some((r) => r.x % fresh.BLOCK_PITCH_PX !== 0), 'loaded: snapped edges in the rebuilt image');
		paint();
		assert.equal(images.length, 2, 'once snapped the image is reused, not rebuilt per frame');
	} finally {
		if (hadDocument) globalThis.document = prevDocument;
		else delete globalThis.document;
	}
});

test('change detection rejects a duration that is not finite and positive', () => {
	const { detail } = windows[0];
	for (const bad of [0, -1, Number.NaN, Number.POSITIVE_INFINITY]) {
		assert.throws(() => layout.detectChangePoints(detail, bad, UNIT), RangeError, `duration ${bad}`);
		assert.throws(() => layout.detectChangePoints(detail, 10, { divisor: bad, clampAtOne: false }), RangeError, `divisor ${bad}`);
	}
	// Control: a valid duration on a too-short waveform is still just no changes.
	const one = { low: [0.5], mid: [0.5], high: [0.5], length: 1 };
	assert.equal(layout.detectChangePoints(one, 10, UNIT).length, 0);
});

test('beat anchors land on the same pixel as the painted grid line, changes stay floored', async () => {
	const math = await loadTypeScriptModule('src/lib/components/rb/wave/wave-math.ts');
	// Fractions on both sides of .5: a floored beat sits a pixel left of its line on the upper half.
	// 2 px/s keeps beats over 10 px apart, so the grid paints every one (WAVEZOOM-02).
	const beats = [10.1, 20.25, 30.35, 40.245, 50.255].map((t, i) => ({ n: (i % 4) + 1, bpm: 120, t }));
	const pxPerS = 2;
	const lines = math.visibleBeatLines(beats, 0, pxPerS, 120, 40).map((l) => l.x);
	assert.equal(lines.length, beats.length, 'CONTROL: every beat paints a line');
	assert.deepEqual(Array.from(layout.beatAnchorsPx(beats, pxPerS)), lines);
	// Control: change anchors keep flooring (the bucket before a change must not reach it).
	assert.deepEqual(Array.from(layout.changeAnchorsPx(Int32Array.from([307, 505]), 1000, 100)), [30, 50]);
});

test('anchors at the row ends still start blocks (Sol P1 4303506953 on PR #6534)', () => {
	const W = 30;
	const pitchesOk = (edges) => {
		for (let b = 1; b < edges.length; b++) {
			const p = edges[b] - edges[b - 1];
			if (p < layout.BLOCK_PITCH_MIN_PX || p > layout.BLOCK_PITCH_MAX_PX) return false;
		}
		return true;
	};
	// A change on pixel 1 starts the first block there; pixel 0 is left unpainted.
	const first = Array.from(layout.snappedBlockEdges(W, [1], []));
	assert.equal(first[0], 1, first.join(','));
	assert.ok(pitchesOk(first), first.join(','));
	// A beat on the last pixel starts a block that runs past the row, like the tail.
	const last = Array.from(layout.snappedBlockEdges(W, [], [W - 1]));
	assert.ok(last.includes(W - 1), last.join(','));
	assert.ok(last.at(-1) >= W && last.at(-1) < W + 3, last.join(','));
	assert.ok(pitchesOk(last), last.join(','));
	// Both at once, with a change on pixel W - 2 too.
	const both = Array.from(layout.snappedBlockEdges(W, [1, W - 2], [W - 1]));
	assert.ok(both[0] === 1 && both.includes(W - 2), both.join(','));
	assert.ok(pitchesOk(both), both.join(','));
	// Control: an anchor on pixel 0 is the existing first edge, so the grid is the old one.
	assert.deepEqual(Array.from(layout.snappedBlockEdges(W, [0], [])), Array.from(render.uniformBlockEdges(W)));
	// Control: anchors off the row are ignored, not clamped onto it.
	assert.deepEqual(Array.from(layout.snappedBlockEdges(W, [], [-1, W])), Array.from(render.uniformBlockEdges(W)));
});

test('the linear-time lookback finds exactly the changes a full rescan finds (Sol P1 4303676528 on PR #6534)', () => {
	const cfg = layout.BLOCK_SNAP_CFG;
	// The pre-fix rescan of every lookback window, kept here as the reference.
	const reference = (bands, durS, norm) => {
		const n = bands.low.length;
		const ptsPerS = n / durS;
		const look = Math.max(1, Math.round(cfg.LOOKBACK_S * ptsPerS));
		const refractory = Math.max(1, Math.ceil(cfg.REFRACTORY_S * ptsPerS));
		const level = [];
		for (let i = 0; i < n; i++) {
			let v = Math.max(bands.low[i], bands.mid[i], bands.high[i]) / norm.divisor;
			if (norm.clampAtOne && v > 1) v = 1;
			level.push(v > 0 ? Math.fround(Math.pow(v, cfg.AMP_GAMMA)) : 0);
		}
		const out = [];
		let last = -refractory;
		let inRise = false;
		for (let i = 1; i < n; i++) {
			let prior = 0;
			for (let j = Math.max(0, i - look); j < i; j++) if (level[j] > prior) prior = level[j];
			const rising = level[i] - prior >= cfg.RISE;
			if (rising && !inRise && i - last >= refractory) {
				out.push(i);
				last = i;
			}
			inRise = rising;
		}
		return out;
	};
	for (const win of windows) {
		const got = Array.from(layout.detectChangePoints(win.detail, win.durS, peakNorm(win.detail)));
		assert.ok(got.length > 0, `CONTROL: ${win.file} has changes`);
		assert.deepEqual(got, reference(win.detail, win.durS, peakNorm(win.detail)), win.file);
	}
	// Seeded noise with long lookbacks (high resolution): ties, plateaus and drops.
	let seed = 7;
	const rnd = () => ((seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648);
	for (const [n, durS] of [[4000, 2], [20000, 2], [3000, 30]]) {
		// Mostly quiet with sparse bursts, quantized so ties occur.
		const q = () => Array.from({ length: n }, () => Math.round((rnd() < 0.01 ? rnd() : rnd() * 0.05) * 16) / 16);
		const bands = { low: q(), mid: q(), high: q(), length: n };
		const got = Array.from(layout.detectChangePoints(bands, durS, UNIT));
		assert.ok(got.length > 0, `CONTROL: ${n} points over ${durS}s has changes`);
		assert.deepEqual(got, reference(bands, durS, UNIT), `${n} points over ${durS}s`);
	}
});

test('a snapped bar across a band-cache tile edge paints the same columns as the whole row', () => {
	const tile = render.BAND_CACHE_CFG.TILE_PX;
	const W = tile * 2;
	// 4 px blocks up to the tile edge leave fewer blocks before it than
	// tile / 3, and the change at tile - 1 starts a bar that straddles it.
	const changes = [];
	for (let x = 0; x < tile - 1; x += 4) changes.push(x);
	changes.push(tile - 1);
	const edges = layout.snappedBlockEdges(W, changes, []);
	const straddler = Array.from(edges).findIndex((e, b) => e < tile && edges[b + 1] - 1 > tile);
	assert.ok(straddler >= 0, 'CONTROL: one bar straddles the tile edge');
	assert.ok(straddler < tile / 3, 'CONTROL: fewer blocks than the fixed pitch puts before the edge');
	const heights = new Float32Array(edges.length - 1).fill(1);
	const detail = { low: [1, 1], mid: [1, 1], high: [1, 1], length: 2 };
	const waveform = { kind: 'mono', preview: detail, detail };
	const blocks = { curve: 'true-peak', variant: 'max', beatPeriodS: null, mirrored: true, snap: true, beats: null };
	const palette = { low: '#1', mid: '#2', high: '#3', mono: '#4' };
	const columns = (x0, x1) => {
		const cov = new Uint8Array(W);
		const cover = (r) => {
			for (let x = Math.max(0, r.x); x < Math.min(x1 - x0, r.x + r.w); x++) cov[x0 + x] = 1;
		};
		const ctx = { fillStyle: '', globalAlpha: 1, fill: (path) => path.rects.forEach(cover), fillRect: (x, y, w, h) => cover({ x, y, w, h }) };
		render.__test_drawBands(ctx, waveform, 1, W, W, ROW_H, palette, 'blocks', blocks, 0, {
			x0, x1, blockColumns: { heights, shares: null, edges }
		});
		return cov;
	};
	const whole = columns(0, W);
	const right = columns(tile, W);
	const tiled = columns(0, tile).map((v, x) => v | right[x]);
	assert.deepEqual(Array.from(tiled), Array.from(whole));
	assert.equal(whole[tile - 1] + whole[tile], 2, 'CONTROL: the straddling bar covers both sides of the edge');
});

test('on a thinned grid only painted beat lines anchor, and each keeps its edge (Sol P1 4426841369 on PR #6534)', async () => {
	const math = await loadTypeScriptModule('src/lib/components/rb/wave/wave-math.ts');
	// 1.5 px per beat: the grid keeps every 4th bar, and the hidden beat before
	// each kept line rounds to 1 px before it, inside the anchor merge gap, where
	// the earlier anchor wins: anchoring it would take the line's edge away.
	const pxPerS = 3;
	const beats = Array.from({ length: 480 }, (_, i) => ({ n: (i % 4) + 1, bpm: 120, t: 1 + i * 0.5 }));
	const W = Math.ceil(241 * pxPerS);
	const anchors = Array.from(layout.beatAnchorsPx(beats, pxPerS));
	assert.ok(anchors.length > 0 && anchors.length < beats.length / 4, `CONTROL: the grid thinned (${anchors.length} of ${beats.length})`);
	const edges = new Set(layout.snappedBlockEdges(W, [], anchors));
	// Every line a scrolled window paints starts a block, at the row's own origin.
	for (const tLeft of [0, 17.3, 63.9, 150.25]) {
		const origin = Math.round(tLeft * pxPerS);
		const lines = math.visibleBeatLines(beats, tLeft, pxPerS, 96 * pxPerS, 40, false, origin);
		assert.ok(lines.length > 0, `CONTROL: the window at ${tLeft} s paints lines`);
		for (const l of lines) assert.ok(edges.has(l.x + origin), `the line at ${l.x + origin} px starts a block (window ${tLeft} s)`);
	}
	// Hidden beats bend nothing: kept lines sit 24 px apart (16 beats of 1.5 px),
	// so between the first and last anchor every block keeps the fixed 3 px pitch.
	for (let i = 1; i < anchors.length; i++) assert.equal(anchors[i] - anchors[i - 1], 24, 'CONTROL: kept lines 24 px apart');
	const row = Array.from(layout.snappedBlockEdges(W, [], anchors)).filter((e) => e >= anchors[0] && e <= anchors.at(-1));
	for (let b = 1; b < row.length; b++) assert.equal(row[b] - row[b - 1], 3, `fixed pitch at ${row[b - 1]} px`);
	// Control: at deck zoom nothing thins, and every beat anchors.
	assert.equal(layout.beatAnchorsPx(beats, 40).length, beats.length);
});

test('variable-tempo slow window snaps every painted beat line (issue #8335)', async () => {
	const math = await loadTypeScriptModule('src/lib/components/rb/wave/wave-math.ts');
	const pxPerS = 22;
	const windowS = 24;
	const widthCss = pxPerS * windowS;
	const slowBeats = 64;
	const slowDurS = slowBeats * 0.5;
	const beats = [];
	for (let i = 0; i < slowBeats; i++) {
		beats.push({ n: (i % 4) + 1, bpm: 120, t: i * 0.5 });
	}
	for (let j = 0; j < 80; j++) {
		beats.push({ n: (j % 4) + 1, bpm: 150, t: slowDurS + j * 0.4 });
	}
	const durS = slowDurS + 80 * 0.4;
	const tLeftSlow = 0;
	const positionMs = (tLeftSlow + windowS / 2) * 1000;
	assert.equal(math.beatLineStrideForWindow(beats, tLeftSlow, pxPerS, widthCss), 1);
	assert.equal(math.beatLineStrideForWindow(beats, 0, pxPerS, Infinity), 4);
	const slowLines = math.visibleBeatLines(beats, tLeftSlow, pxPerS, widthCss, 40, false, 0);
	const slowXs = new Set(slowLines.map((l) => l.x));
	for (const x of [121, 132, 143]) assert.ok(slowXs.has(x), `CONTROL: slow window paints beat at ${x} px`);
	const anchors = Array.from(layout.beatAnchorsPx(beats, pxPerS, false, 1));
	for (const x of [121, 132, 143]) assert.ok(anchors.includes(x), `stride-1 anchor at ${x} px`);
	const detail = { length: 1200, low: Array(1200).fill(0.3), mid: Array(1200).fill(0.3), high: Array(1200).fill(0.3) };
	const mono = '#cfcfcf';
	const lines = [];
	const blockLefts = new Set();
	const palette = {
		bg: '#000', low: '#1', mid: '#2', high: '#3', mono, tick: '#4',
		cueHotCue: '#35c04f', cueLoop: '#e8a13a', cueMemory: '#ff3b30', cueOutline: '#c8cdd2', phrase: '#888'
	};
	const ctx = {
		fillStyle: '',
		globalAlpha: 1,
		imageSmoothingEnabled: true,
		fillRect(x, y, w, h) {
			if (this.globalAlpha === 0.38 || this.globalAlpha === 0.55) lines.push(x);
			if (this.fillStyle === mono && this.globalAlpha === 1) blockLefts.add(x);
		},
		fill() {},
		drawImage() {}
	};
	render.drawWaveRow(ctx, {
		widthCss, heightCss: ROW_H, positionMs, durationMs: durS * 1000,
		anlz: anlzOf(detail, beats), palette, pitch: 1, windowS, playheadFrac: 0.5, loop: null,
		waveformDesign: 'blocks', blocksSnap: true
	});
	for (const x of [121, 132, 143]) {
		assert.ok(lines.includes(x), `painted line at ${x} px`);
		assert.ok(blockLefts.has(x), `painted line at ${x} px starts a block`);
	}
	const fastLeft = slowDurS + 4;
	const fastStride = math.beatLineStrideForWindow(beats, fastLeft, pxPerS, widthCss);
	assert.equal(fastStride, 4);
	const fastLines = math.visibleBeatLines(beats, fastLeft, pxPerS, widthCss, 40, false, Math.round(fastLeft * pxPerS));
	const fastEdges = new Set(
		layout.snappedBlockEdges(Math.ceil(durS * pxPerS), [], layout.beatAnchorsPx(beats, pxPerS, false, fastStride))
	);
	for (const l of fastLines) assert.ok(fastEdges.has(l.x + Math.round(fastLeft * pxPerS)), `thinned line at ${l.x} px has an edge`);
});
