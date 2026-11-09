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

test('the stem row requires the main row geometry instead of drawing at a guessed scale', () => {
	const rec = recordingCtx(100, 12);
	const frame = {
		envelope: [1, 1, 1],
		scrollPx: 0,
		durationMs: 10_000,
		pitch: 1,
		width: rec.width,
		height: rec.height,
		color: '#fff'
	};
	assert.throws(
		() => render.drawStemWaveRow(rec.ctx, { ...frame, playheadFrac: 0.5 }),
		RangeError,
		'windowS is required'
	);
	assert.throws(
		() => render.drawStemWaveRow(rec.ctx, { ...frame, windowS: 24 }),
		RangeError,
		'playheadFrac is required'
	);
	assert.throws(
		() => render.drawStemWaveRow(rec.ctx, { ...frame, windowS: 24, playheadFrac: 2 }),
		RangeError
	);
});

test('drawStemWaveRow paints non-empty bars from a fixture envelope', () => {
	const envelope = Array.from({ length: 100 }, (_, i) => (i === 50 ? 1 : 0));
	const rec = recordingCtx(100, 12);
	render.drawStemWaveRow(rec.ctx, {
		envelope,
		scrollPx: 50,
		windowS: 24,
		playheadFrac: 0.5,
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
		windowS: 24,
		playheadFrac: 0.5,
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
	assert.deepEqual(
		paintedColumns({ positionS: 60, spikeS: 60 }),
		[120, 121, 122, 123, 124, 125, 126, 127, 128, 129]
	);
});

test('stem row places a later peak at the main row px-per-second, not the window width', () => {
	// 10 s ahead of the playhead at 10 px/s lands 100 px right of center.
	assert.deepEqual(
		paintedColumns({ positionS: 60, spikeS: 70 }),
		[220, 221, 222, 223, 224, 225, 226, 227, 228, 229]
	);
	// Just outside the visible window (12 s + 1 s either side): nothing painted.
	assert.deepEqual(paintedColumns({ positionS: 60, spikeS: 73 }), []);
	assert.deepEqual(paintedColumns({ positionS: 60, spikeS: 47 }), []);
});

test('stem row widens its window with pitch exactly like the main row', () => {
	// pitch 2 shows 48 s of track: 5 px/s, so 10 s ahead is 50 px right of center.
	assert.deepEqual(
		paintedColumns({ positionS: 60, spikeS: 70, pitch: 2 }),
		[170, 171, 172, 173, 174]
	);
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
			windowS: 24,
			playheadFrac: 0.5,
			durationMs,
			pitch,
			width: rec.width,
			height: rec.height,
			color: '#4fb2ff'
		});
		assert.equal(
			rec.fills.length,
			0,
			`durationMs=${durationMs} pitch=${pitch} must not invent a time scale`
		);
	}
	// Control: the same flat envelope on a real 10 s track at pitch 1 does paint.
	const rec = recordingCtx(W, 12);
	render.drawStemWaveRow(rec.ctx, {
		envelope: Array.from({ length: 10 }, () => 1),
		scrollPx: 0,
		windowS: 24,
		playheadFrac: 0.5,
		durationMs: 10_000,
		pitch: 1,
		width: rec.width,
		height: rec.height,
		color: '#4fb2ff'
	});
	assert.equal(rec.fills.length, 100, 'track seconds 0..10 sit right of center at 10 px/s');
});

// PERF-PAINT-02 (issue #10221): StemWaveRow reads its --rb-stem-<id> token once
// per theme/palette/skin/stem change, not on every scrollPx paint. The harness
// mirrors StemWaveRow's two effects on the REAL Svelte scheduler and uiPrefs.
const ROW_HARNESS = [
	"export { uiPrefs } from '$lib/rb/prefs.svelte';",
	"import { flushSync } from 'svelte';",
	"import { uiPrefs } from '$lib/rb/prefs.svelte';",
	"import { stemWaveRowColorFromElement } from '$lib/components/rb/wave/stem-waveform-ui';",
	'export function mountRow(el: Element, stem: string) {',
	'	const st = $state({ scrollPx: 0 });',
	'	let stemColor: string | null = $state(null);',
	'	const painted: string[] = [];',
	'	const stop = $effect.root(() => {',
	'		$effect(() => { void uiPrefs.theme; void uiPrefs.wave_palette; void uiPrefs.ui_skin; stemColor = stemWaveRowColorFromElement(el, stem as never); });',
	'		$effect(() => { void st.scrollPx; if (stemColor === null) return; painted.push(stemColor); });',
	'	});',
	'	flushSync();',
	'	return { painted, stop, scroll(px: number) { st.scrollPx = px; flushSync(); }, flush() { flushSync(); } };',
	'}'
].join('\n');

function countingComputedStyle(tokenRef) {
	const calls = { n: 0 };
	globalThis.getComputedStyle = () => {
		calls.n += 1;
		return { getPropertyValue: () => tokenRef.value };
	};
	return calls;
}

test('if 60 stem row paints with a moving scrollPx call getComputedStyle more than once then broken', async () => {
	const { loadRuneModule } = await import('./load-rune-module.mjs');
	const live = await loadRuneModule(ROW_HARNESS);
	const token = { value: '#11aa22' };
	const calls = countingComputedStyle(token);
	const row = live.mountRow({}, 'drums');
	for (let i = 1; i < 60; i += 1) row.scroll(i * 3);
	row.stop();
	assert.equal(row.painted.length, 60, 'every scroll step must paint');
	assert.equal(calls.n, 1, `token read ${calls.n} times over 60 paints, expected 1`);
});

test('if a ui_skin change does not re-read the stem token exactly once and repaint with it then broken', async () => {
	const { loadRuneModule } = await import('./load-rune-module.mjs');
	const live = await loadRuneModule(ROW_HARNESS);
	const token = { value: '#11aa22' };
	const calls = countingComputedStyle(token);
	const row = live.mountRow({}, 'bass');
	token.value = '#ff0000';
	live.uiPrefs.ui_skin = live.uiPrefs.ui_skin === 'light' ? 'mono-dev' : 'light';
	row.flush();
	row.scroll(42);
	row.stop();
	assert.equal(calls.n, 2, `expected one read at mount and one per skin change, got ${calls.n}`);
	assert.equal(
		row.painted.at(-1).toLowerCase(),
		'#ff0000',
		'next paint must use the new skin colour'
	);
});

test('if stemWaveRowColorFromElement colours differ from stemWaveRowColor for any stem then broken', async () => {
	const ui = await loadTypeScriptModule('src/lib/components/rb/wave/stem-waveform-ui.ts');
	for (const token of ['', ' #123456 ']) {
		globalThis.getComputedStyle = () => ({ getPropertyValue: () => token });
		for (const stem of ['vocal', 'instrumental', 'drums', 'bass', 'other']) {
			assert.equal(
				ui.stemWaveRowColorFromElement({}, stem),
				ui.stemWaveRowColor(stem, token),
				stem
			);
		}
	}
});
