import assert from 'node:assert/strict';
import { before, describe, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// TEMPOLANE-8342 - the tempo curve (the maintainer's pick, Tue 3 Nov 2026) on the deck
// strip and the main waveform paints ONLY the daemon's /tempo-stability
// segments: height is percent off nominal, red slower with a turtle, green
// faster with a rabbit, dashed gray unstable, nothing for a steady track.
// Every detection test has a quiet control beside it.
//
// Regression lines:
// - if a steady track (no segments) paints any tempo pixel then broken
// - if a faster stretch paints red or a slower one green, or the icon disagrees, then broken
// - if the curve height does not follow percent off nominal on the chosen scale then broken
// - if a return toward nominal gets its own opposite icon then broken
// - if an unstable stretch paints solid or with an icon then broken
// - if a hover number has no "vs nominal" or BPM beside it then broken

let strip;
let marks;
let words;

before(async () => {
	strip = await loadTypeScriptModule('src/lib/components/rb/deck/strip-waveform-render.ts');
	marks = await loadTypeScriptModule('src/lib/components/rb/wave/tempo-marks.ts');
	words = await loadTypeScriptModule('src/lib/rb/tempo-stability.ts');
});

/** A recording 2D context: fills and strokes kept with the style in force. */
function recordingCtx() {
	const fills = [];
	const strokes = [];
	const icons = [];
	let path = [];
	const ctx = {
		fillStyle: '',
		strokeStyle: '',
		lineWidth: 1,
		globalAlpha: 1,
		dash: [],
		setLineDash(d) { ctx.dash = d; },
		save() {}, restore() {}, translate() {}, scale() {}, arc() {}, ellipse() {}, closePath() {}, clearRect() {}, strokeRect() {},
		fillRect(x, y, w, h) { fills.push({ x, y, w, h, fillStyle: ctx.fillStyle }); },
		beginPath() { path = []; },
		moveTo(x, y) { path.push([x, y]); },
		lineTo(x, y) { path.push([x, y]); },
		stroke(p) {
			if (p !== undefined) icons.push({ d: p.d, color: ctx.strokeStyle });
			else strokes.push({ points: path, color: ctx.strokeStyle, dash: ctx.dash, width: ctx.lineWidth });
		},
		fill() { fills.push({ path, fillStyle: ctx.fillStyle }); }
	};
	return { ctx, fills, strokes, icons };
}

before(() => {
	globalThis.Path2D = class { constructor(d) { this.d = d; } };
});

const NOMINAL = 128;
const COLORS = () => marks.TEMPO_LANE_COLORS.dark;
const seg = (over) => ({ start_s: 120, end_s: 160, bpm_start: 131.84, bpm_end: 131.84, kind: 'jump', severity: 0.2, direction: 'faster', ...over });
const data = (segments, largest = 3) => ({ nominal_bpm: NOMINAL, largest_change_pct: largest, segments });
const BAND = { top: 3, height: 32, lineWidth: 2, iconPx: 16 };
const colorStrokes = (strokes) => strokes.filter((s) => Object.values(COLORS()).includes(s.color));
const RABBIT_HEAD = 'M20 8.54V4a2 2 0 1 0-4 0v3';
const TURTLE_SHELL = 'M16.93 10H20a2 2 0 0 1 0 4H2';

describe('tempo curve painter', () => {
	test('a steady answer paints nothing, not even the scrim', () => {
		for (const segments of [[], [seg({ kind: 'steady', direction: 'none', severity: 0, bpm_start: 128, bpm_end: 128 })]]) {
			const { ctx, fills, strokes } = recordingCtx();
			marks.drawTempoCurve(ctx, data(segments), 0, 1, 600, BAND, COLORS(), 'fixed_10');
			assert.equal(fills.length + strokes.length, 0);
		}
	});

	test('a faster jump steps up in green with a rabbit, then returns to nominal', () => {
		const { ctx, strokes, icons } = recordingCtx();
		marks.drawTempoCurve(ctx, data([seg({})]), 100, 10, 600, BAND, COLORS(), 'fixed_10');
		const green = colorStrokes(strokes).filter((s) => s.color === COLORS().faster);
		assert.ok(green.length > 0);
		const base = BAND.top + BAND.height / 2;
		const [, up] = green[0].points;
		assert.equal(up[0], 200, 'x = (120 - 100) * 10');
		assert.ok(Math.abs(base - up[1] - 0.3 * (BAND.height / 2)) < 0.01, '3 percent of a 10 percent scale is 0.3 of the half band, upward');
		assert.ok(strokes.some((s) => s.points.length === 2 && s.points[1][1] === base), 'the episode ends back on the nominal line');
		assert.ok(icons.some((i) => i.d === RABBIT_HEAD && i.color === COLORS().faster));
		assert.ok(!icons.some((i) => i.d === TURTLE_SHELL));
	});

	test('slower is red with a turtle and goes below the nominal line', () => {
		const { ctx, strokes, icons } = recordingCtx();
		marks.drawTempoCurve(ctx, data([seg({ direction: 'slower', bpm_start: 124.16, bpm_end: 124.16 })]), 100, 10, 600, BAND, COLORS(), 'fixed_10');
		const red = colorStrokes(strokes).find((s) => s.color === COLORS().slower);
		assert.ok(red.points[1][1] > BAND.top + BAND.height / 2);
		assert.ok(icons.some((i) => i.d === TURTLE_SHELL && i.color === COLORS().slower));
	});

	test('one icon per change episode: a rabbit is not followed by a turtle for the way back', () => {
		const { ctx, icons } = recordingCtx();
		const ramp = seg({ start_s: 110, end_s: 120, kind: 'ramp', bpm_start: 128.2, bpm_end: 131.8 });
		const held = seg({ start_s: 120, end_s: 150 });
		const down = seg({ start_s: 150, end_s: 155, kind: 'ramp', direction: 'slower', bpm_start: 131.8, bpm_end: 128.4 });
		marks.drawTempoCurve(ctx, data([ramp, held, down]), 100, 10, 900, BAND, COLORS(), 'fixed_10');
		assert.equal(icons.filter((i) => i.d === RABBIT_HEAD).length, 1);
		assert.equal(icons.filter((i) => i.d === TURTLE_SHELL).length, 0);
	});

	test('relative scale fills the band with the track\'s own biggest change, never below 2 percent', () => {
		assert.equal(marks.tempoCurveFullPct('fixed_10', 3), 10);
		assert.equal(marks.tempoCurveFullPct('relative', 3), 3);
		assert.equal(marks.tempoCurveFullPct('relative', 0.5), 2);
		const { ctx, strokes } = recordingCtx();
		marks.drawTempoCurve(ctx, data([seg({})], 3), 100, 10, 600, BAND, COLORS(), 'relative');
		const [, up] = colorStrokes(strokes).find((s) => s.color === COLORS().faster).points;
		assert.ok(Math.abs(up[1] - BAND.top) < 0.05, 'a 3 percent move on a 3 percent track reaches the band top');
	});

	test('color follows position: a rising ramp still below nominal is red, not green', () => {
		const { ctx, strokes } = recordingCtx();
		const rising = seg({ kind: 'ramp', direction: 'faster', bpm_start: 120, bpm_end: 126 });
		marks.drawTempoCurve(ctx, data([rising]), 100, 10, 600, BAND, COLORS(), 'fixed_10');
		assert.ok(colorStrokes(strokes).some((s) => s.color === COLORS().slower));
		assert.ok(!colorStrokes(strokes).some((s) => s.color === COLORS().faster));
	});

	test('unstable is dashed gray with no icon', () => {
		const { ctx, strokes, icons } = recordingCtx();
		marks.drawTempoCurve(ctx, data([seg({ kind: 'unstable', direction: 'none', bpm_start: 126, bpm_end: 129 })]), 100, 10, 600, BAND, COLORS(), 'fixed_10');
		const gray = strokes.find((s) => s.color === COLORS().unstable);
		assert.ok(gray.dash.length > 0);
		assert.equal(icons.length, 0);
	});

	test('the strip draws its curve below the vocal bar, scaled with the backing height', () => {
		const { ctx, fills } = recordingCtx();
		strip.drawStripWaveform(ctx, {
			widthPx: 400, heightPx: 80, durationMs: 200_000, waveform: null, vocals: null, loop: null, loopCues: [],
			tempo: data([seg({})]), tempoColors: COLORS(), tempoScale: 'fixed_10'
		});
		const scrim = fills.find((f) => f.w === 400);
		assert.equal(scrim.y, 10, '(4 px vocal bar + 1) at the 40 px reference, doubled');
		assert.ok(Math.abs(scrim.h - 80 / 3) < 0.01, 'a third of the strip');
	});

	test('a null read paints no tempo at all on the strip', () => {
		const { ctx, fills, strokes } = recordingCtx();
		strip.drawStripWaveform(ctx, { widthPx: 400, heightPx: 40, durationMs: 200_000, waveform: null, vocals: null, loop: null, loopCues: [], tempo: null });
		assert.equal(fills.length + strokes.length, 0);
	});
});

describe('tempo lane hover and cache key', () => {
	const ok = (over) => ({
		stable_id: 'a'.repeat(40),
		status: 'ok',
		reason: null,
		beatgrid_source: 'rekordbox',
		nominal_bpm: 128,
		verdict: 'changes',
		largest_change_pct: 3,
		change_count: 1,
		grid_class: 'variable_tempo',
		grid_quality_message: 'x',
		segments: [
			{ start_s: 0, end_s: 120, bpm: 128, bpm_start: 128, bpm_end: 128, delta_pct: 0, kind: 'steady', severity: 0, direction: 'none' },
			{ start_s: 120, end_s: 200, bpm: 131.84, bpm_start: 131.84, bpm_end: 131.84, delta_pct: 3, kind: 'jump', severity: 0.2, direction: 'faster' }
		],
		...over
	});

	test('every number says what it is', () => {
		const title = words.tempoStabilityTitle({ state: 'ready', value: ok({}) });
		assert.match(title, /nominal 128\.00 BPM/);
		assert.match(title, /3\.0% from nominal/);
		assert.match(title, /2:00-3:20 held at 131\.84 BPM \(\+3\.0% vs nominal\)/);
		assert.doesNotMatch(title, /0:00-2:00/, 'steady stretches are not listed');
	});

	test('not analyzed never reads steady; nothing read yet says nothing', () => {
		const title = words.tempoStabilityTitle({
			state: 'ready',
			value: ok({ status: 'not_analyzed', reason: 'no beats', verdict: null, nominal_bpm: null, largest_change_pct: null, segments: [] })
		});
		assert.match(title, /not analyzed - no beats/);
		assert.doesNotMatch(title, /steady/);
		assert.equal(words.tempoStabilityTitle({ state: 'loading' }), null);
	});

	test('the key moves when the served grid does', () => {
		const grid = { source: 'own', beats: [{ t: 0.1 }, { t: 0.6 }] };
		const key = words.tempoStabilityKey('a', grid);
		assert.equal(key, words.tempoStabilityKey('a', { source: 'own', beats: [{ t: 0.1 }, { t: 0.6 }] }));
		assert.notEqual(key, words.tempoStabilityKey('a', { source: 'rekordbox', beats: grid.beats }));
		assert.notEqual(key, words.tempoStabilityKey('a', { source: 'own', beats: [{ t: 0.102 }, { t: 0.602 }] }), 'grid adjust');
		const three = { source: 'own', beats: [{ t: 0.1 }, { t: 0.6 }, { t: 1.1 }] };
		const interior = { source: 'own', beats: [{ t: 0.1 }, { t: 0.65 }, { t: 1.1 }] };
		assert.notEqual(words.tempoStabilityKey('a', three), words.tempoStabilityKey('a', interior), 'interior beat edit');
	});

	test('a failed read is retried only after the backoff, never at once', () => {
		assert.equal(words.tempoStabilityNeedsFetch(undefined, 0), true);
		assert.equal(words.tempoStabilityNeedsFetch({ state: 'loading' }, 0), false);
		const failed = { state: 'error', message: 'boom', at: 1000 };
		assert.equal(words.tempoStabilityNeedsFetch(failed, 1000), false, 'no instant loop');
		assert.equal(words.tempoStabilityNeedsFetch(failed, 1000 + words.TEMPO_STABILITY_RETRY_MS), true);
		assert.equal(words.tempoStabilityNeedsFetch({ state: 'ready', value: {} }, 1e12), false);
	});

	test('an incomplete answer fails loudly instead of reading as zero or no beatgrid', () => {
		assert.throws(() => words.tempoStabilityTitle({ state: 'ready', value: ok({ change_count: null }) }), /always carries/);
		assert.throws(
			() => words.tempoStabilityTitle({ state: 'ready', value: ok({ status: 'not_analyzed', reason: null, verdict: null }) }),
			/always says why/
		);
	});
});
