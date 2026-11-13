import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let ui;
let follow;
let render;

before(async () => {
	globalThis.Path2D ??= class {
		rect() {}
	};
	ui = await loadTypeScriptModule('src/lib/components/rb/wave/stem-waveform-ui.ts');
	follow = await loadTypeScriptModule('src/lib/components/rb/wave/wave-stem-follow.ts');
	render = await loadTypeScriptModule('src/lib/components/rb/wave/render.ts');
});

function neutral() {
	const c = () => ({ muted: false, solo: false, gain: 0.5 });
	return { vocal: c(), instrumental: c(), drums: c(), bass: c(), other: c() };
}

function fakeCtx() {
	const calls = [];
	const ctx = {
		fillStyle: '#000',
		strokeStyle: '#000',
		globalAlpha: 1,
		lineWidth: 1,
		clearRect: (...a) => calls.push(['clearRect', ...a]),
		fillRect(x, y, w, h) {
			calls.push(['fillRect', x, y, w, h, this.fillStyle]);
		},
		beginPath() {},
		moveTo() {},
		lineTo() {},
		stroke() {},
		fill() {},
		save() {},
		restore() {}
	};
	return { ctx, calls };
}

function frame(over = {}) {
	return {
		enabled: true,
		stem: 'vocal',
		entry: { status: 'ready', envelope: [1, 1, 1, 1] },
		color: '#ff0000',
		scrollPx: 50,
		durationMs: 10_000,
		pitch: 1,
		width: 100,
		height: 80,
		windowS: 10,
		playheadFrac: 0.5,
		playheadTone: 'now',
		playheadTimeMs: 0,
		...over
	};
}

test('soleAudibleStem: one exclusive solo wins, several or muted or original or none give null', () => {
	/** [if] the solo state varies [then] only one unmuted exclusive solo on stems playback names a stem, [else stop]. */
	const c = neutral();
	assert.equal(ui.soleAudibleStem(c, 'stems'), null);
	c.vocal.solo = true;
	assert.equal(ui.soleAudibleStem(c, 'stems'), 'vocal');
	assert.equal(ui.soleAudibleStem(c, 'original'), null);
	c.drums.solo = true;
	assert.equal(ui.soleAudibleStem(c, 'stems'), null);
	c.drums.solo = false;
	c.vocal.muted = true;
	assert.equal(ui.soleAudibleStem(c, 'stems'), null);
});

test('ACA quick mode: three parts muted and one audible names that part', () => {
	/** [if] every part but vocal is muted with no solo [then] the sole audible stem is vocal, held original gives null, [else stop]. */
	const c = neutral();
	c.instrumental.muted = true;
	c.drums.muted = true;
	c.bass.muted = true;
	c.other.muted = true;
	assert.equal(ui.soleAudibleStem(c, 'stems'), 'vocal');
	assert.equal(ui.soleAudibleStem(c, 'original'), null);
	const r = neutral();
	r.instrumental.muted = true;
	assert.equal(ui.soleAudibleStem(r, 'stems', 'roformer2'), 'vocal');
});

test('INST and ALL: two or more audible parts and no solo give no sole stem', () => {
	/** [if] no part is soloed and two or more parts are audible [then] soleAudibleStem is null so the mix paints, [else stop]. */
	const c = neutral();
	assert.equal(ui.soleAudibleStem(c, 'stems'), null);
	c.vocal.muted = true;
	assert.equal(ui.soleAudibleStem(c, 'stems'), null);
});

test('a single solo still wins over unmuted parts', () => {
	/** [if] one part is soloed and others are unmuted [then] the soloed part is the sole stem, [else stop]. */
	const c = neutral();
	c.drums.solo = true;
	assert.equal(ui.soleAudibleStem(c, 'stems'), 'drums');
});

test('pref on with a ready vocal envelope paints the vocal colour at full row height and skips the mix', () => {
	/** [if] the pref is on and the envelope is ready [then] the stem paints at main height and the mix does not, [else stop]. */
	const { ctx, calls } = fakeCtx();
	const bodies = [];
	const out = follow.paintMainWaveWithStemFollow(ctx, frame(), (body) => {
		bodies.push(body);
		body?.(ctx);
	});
	assert.equal(out, 'stem');
	assert.equal(bodies.length, 1);
	assert.equal(typeof bodies[0], 'function', 'the stem replaces the mix body, not the row');
	assert.equal(
		calls.filter((c) => c[0] === 'clearRect').length,
		0,
		'the stem body keeps the row background'
	);
	const bars = calls.filter((c) => c[0] === 'fillRect' && c[5] === '#ff0000');
	assert.ok(bars.length > 0);
	assert.equal(Math.max(...bars.map((b) => b[4])), 79);
});

test('envelope not ready paints the mix, and the next frame with a ready envelope switches to the stem', () => {
	/** [if] the envelope is loading then ready [then] the first frame paints the mix and the next the stem, [else stop]. */
	const { ctx } = fakeCtx();
	let mix = 0;
	const paint = (body) => {
		if (body === undefined) mix++;
	};
	assert.equal(
		follow.paintMainWaveWithStemFollow(ctx, frame({ entry: { status: 'loading' } }), paint),
		'mix'
	);
	assert.equal(follow.paintMainWaveWithStemFollow(ctx, frame({ entry: undefined }), paint), 'mix');
	assert.equal(mix, 2);
	assert.equal(follow.paintMainWaveWithStemFollow(ctx, frame(), paint), 'stem');
	assert.equal(mix, 2);
});

test('pref off paints the mix regardless of solo', () => {
	/** [if] the pref is off with a sole stem and ready envelope [then] only the mix paint runs, [else stop]. */
	const { ctx, calls } = fakeCtx();
	let mix = 0;
	assert.equal(
		follow.paintMainWaveWithStemFollow(ctx, frame({ enabled: false }), () => mix++),
		'mix'
	);
	assert.equal(mix, 1);
	assert.equal(calls.length, 0);
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

/** Records fills, fillRects and strokes in paint order, with the style in force. */
function rowCtx() {
	const sequence = [];
	const ctx = {
		fillStyle: '',
		strokeStyle: '',
		lineWidth: 1,
		globalAlpha: 1,
		beginPath() {},
		moveTo() {},
		lineTo() {},
		closePath() {},
		save() {},
		restore() {},
		setLineDash() {},
		clearRect: () => sequence.push({ kind: 'clearRect' }),
		fill(path) {
			if (path === undefined) sequence.push({ kind: 'fill', style: ctx.fillStyle });
		},
		stroke(path) {
			if (path === undefined) sequence.push({ kind: 'stroke', style: ctx.strokeStyle });
		},
		fillRect(x, y, w, h) {
			sequence.push({ kind: 'fillRect', style: ctx.fillStyle, x, w, h });
		},
		strokeRect() {
			sequence.push({ kind: 'strokeRect', style: ctx.strokeStyle });
		}
	};
	return { ctx, sequence };
}

test('a followed stem keeps the beat grid, loop, cue markers and playhead on the main row', () => {
	/** [if] wave_stem_follow paints a soloed stem [then] the row still paints its grid, engaged loop, hot and memory cues and playhead over the stem, [else stop]. */
	const { ctx, sequence } = rowCtx();
	const cue = (kind, slot, inMs) => ({
		kind,
		slot,
		in_ms: inMs,
		out_ms: null,
		is_loop: false,
		active_loop: false,
		beat_loop_size: null,
		color_table_index: null,
		comment: null
	});
	const beats = Array.from({ length: 48 }, (_, i) => ({ n: (i % 4) + 1, bpm: 120, t: i * 0.5 }));
	const anlz = {
		stable_id: 'a'.repeat(40),
		points: 4,
		waveform: {
			kind: 'tri',
			preview: null,
			detail: { length: 4, low: [1, 1, 1, 1], mid: [1, 1, 1, 1], high: [1, 1, 1, 1] }
		},
		beatgrid: { source: 'rekordbox', beat_count: beats.length, beats },
		cues: [cue('hot_cue', 'A', 4_000), cue('memory', null, 6_000)],
		phrases: [],
		vocals: { status: 'not_analyzed' }
	};
	const loop = { in_ms: 2_000, out_ms: 3_000, engaged: true, beat_length: null };
	const out = follow.paintMainWaveWithStemFollow(
		ctx,
		frame({ scrollPx: 50, durationMs: 10_000, width: 100, height: 80, windowS: 10 }),
		(body) =>
			render.drawWaveRow(ctx, {
				widthCss: 100,
				heightCss: 80,
				positionMs: 5_000,
				durationMs: 10_000,
				anlz,
				palette: PALETTE,
				pitch: 1,
				loop,
				windowS: 10,
				playheadFrac: 0.5,
				body
			})
	);
	assert.equal(out, 'stem');
	const at = (pred) => sequence.findIndex(pred);
	const stem = at((c) => c.kind === 'fillRect' && c.style === '#ff0000');
	assert.ok(stem >= 0, 'the stem envelope paints');
	const grid = at((c) => c.kind === 'fillRect' && c.style === PALETTE.tick);
	assert.ok(grid > stem, 'the beat grid paints over the stem');
	assert.ok(
		at((c) => c.kind === 'fill' && c.style === PALETTE.cueHotCue) > stem,
		'the hot cue marker paints over the stem'
	);
	assert.ok(
		at((c) => c.kind === 'fill' && c.style === PALETTE.cueMemory) > stem,
		'the memory cue marker paints over the stem'
	);
	assert.ok(
		at(
			(c) =>
				c.kind === 'fillRect' && typeof c.style === 'string' && c.style.includes('232, 161, 58')
		) > stem,
		'the engaged loop paints over the stem'
	);
	assert.ok(
		at((c) => c.kind === 'fillRect' && c.style === render.PLAYHEAD_COLORS.now) > stem,
		'the playhead paints last'
	);
	assert.ok(
		sequence.some((c) => c.kind === 'fillRect' && c.style === PALETTE.bg && c.w === 100),
		'the row background paints under the stem'
	);
	assert.equal(
		sequence.filter((c) => c.kind === 'fillRect' && c.style === PALETTE.low).length,
		0,
		'the mix bands do not paint under a followed stem'
	);
});
