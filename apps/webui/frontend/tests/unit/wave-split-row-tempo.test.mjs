/**
 * Requirements: TEMPOLANE-8342b, TEMPOLANE-8342c.
 *
 * A split row paints each deck's own tempo curve in its half. Regression lines:
 * - if a non-steady partner has no tempo curve in the top half then broken
 * - CONTROL: if hideTempo still paints the partner curve then the toggle lies
 * - if the current deck loses its curve from the bottom half then broken
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;
const canvases = [];

function recording() {
	const strokes = [];
	const fills = [];
	const images = [];
	let path = [];
	const ctx = new Proxy(
		{
			fillStyle: '',
			strokeStyle: '',
			lineWidth: 1,
			globalAlpha: 1,
			dash: [],
			setLineDash(dash) {
				ctx.dash = [...dash];
			},
			beginPath() {
				path = [];
			},
			moveTo(x, y) {
				path.push([x, y]);
			},
			lineTo(x, y) {
				path.push([x, y]);
			},
			stroke() {
				strokes.push({ points: [...path], color: ctx.strokeStyle, dash: [...ctx.dash] });
			},
			fillRect(x, y, width, height) {
				fills.push({ x, y, width, height, fillStyle: ctx.fillStyle });
			},
			drawImage(image) {
				images.push(image);
			}
		},
		{
			get: (target, key) => (key in target ? target[key] : () => {}),
			set: (target, key, value) => ((target[key] = value), true)
		}
	);
	return { ctx, strokes, fills, images };
}

before(async () => {
	globalThis.Path2D = class {
		constructor(d) {
			this.d = d;
		}
		rect() {}
		moveTo() {}
		lineTo() {}
	};
	globalThis.document = {
		createElement() {
			const rec = recording();
			const canvas = { width: 0, height: 0, rec, getContext: () => rec.ctx };
			canvases.push(canvas);
			return canvas;
		}
	};
	mod = await loadTypeScriptModule('tests/unit/fixtures/wave-split-row-sections-entry.ts');
});

const TEMPO_COLORS = { slower: '#d12a34', faster: '#19c773', unstable: '#978fa5' };
const PALETTE = {
	bg: '#101010',
	low: '#e8a13a',
	mid: '#4f8fff',
	high: '#f0f0f0',
	mono: '#f0f0f0',
	vocal: '#4fb2ff',
	tick: '#ccc',
	cueHotCue: '#35c04f',
	cueLoop: '#e8a13a',
	cueMemory: '#ff3b30',
	cueOutline: '#c8cdd2',
	phrase: '#888',
	sections: {
		edge: '#00aaff',
		rise: '#ffaa00',
		drop: '#ff0055',
		calm: '#8877cc',
		body: '#6688aa',
		neutral: '#808080',
		text: '#eeeeee'
	}
};
const detail = {
	length: 64,
	low: Array(64).fill(0.5),
	mid: Array(64).fill(0.5),
	high: Array(64).fill(0.5)
};
const beats = Array.from({ length: 8 }, (_, index) => ({
	n: (index % 4) + 1,
	bpm: 128,
	t: 24 + index * 0.5
}));
const ANLZ = {
	stable_id: 'a'.repeat(40),
	points: 64,
	waveform: { kind: 'tri', preview: detail, detail },
	beatgrid: { source: 'rekordbox', beat_count: beats.length, beats },
	tempo_changes: [],
	cues: [],
	phrases: [],
	vocals: { status: 'not_analyzed' }
};
const TEMPO = {
	nominal_bpm: 128,
	largest_change_pct: 3,
	segments: [
		{
			start_s: 25,
			end_s: 35,
			bpm_start: 131.84,
			bpm_end: 131.84,
			kind: 'jump',
			severity: 0.2,
			direction: 'faster'
		}
	]
};
const HALF_HEIGHT = 30;

function frame(overrides = {}) {
	return {
		widthCss: 240,
		heightCss: HALF_HEIGHT,
		positionMs: 30_000,
		durationMs: 120_000,
		anlz: ANLZ,
		palette: PALETTE,
		pitch: 1,
		loop: null,
		windowS: 24,
		playheadFrac: 0.5,
		tempoColors: TEMPO_COLORS,
		tempoScale: 'fixed_10',
		...overrides
	};
}

function paint(top, bottom) {
	const first = canvases.length;
	const target = recording();
	mod.paintSplitRow(
		{},
		target.ctx,
		top,
		bottom,
		{ top: 'A', bottom: 'B', color: '#fff', line: '#888' },
		1
	);
	const [topCanvas, bottomCanvas] = canvases.slice(first, first + 2);
	assert.deepEqual(
		target.images.slice(0, 2),
		[topCanvas, bottomCanvas],
		'the recorded halves are composed in top/bottom order'
	);
	return { topCanvas, bottomCanvas };
}

function tempoStrokes(canvas) {
	return canvas.rec.strokes.filter((stroke) => Object.values(TEMPO_COLORS).includes(stroke.color));
}

test('a non-steady partner paints its tempo curve inside the top half', () => {
	const { topCanvas, bottomCanvas } = paint(frame({ tempo: TEMPO }), frame());
	assert.ok(
		topCanvas.rec.fills.some((fill) => fill.width > 0 && fill.height > 0 && fill.fillStyle),
		'the scratch recorder retains fill rectangles and their fill style'
	);
	const strokes = tempoStrokes(topCanvas);
	assert.ok(strokes.length > 0, 'the top partner frame paints its tempo-colored curve');
	assert.ok(
		strokes.flatMap((stroke) => stroke.points).every(([, y]) => y < HALF_HEIGHT),
		'top curve coordinates stay above the split line'
	);
	assert.equal(
		tempoStrokes(bottomCanvas).length,
		0,
		'CONTROL: the bottom frame has no tempo answer in this paint'
	);
});

test('hideTempo suppresses the partner tempo curve in the top half', () => {
	const { topCanvas } = paint(frame({ tempo: TEMPO, hideTempo: true }), frame());
	assert.equal(tempoStrokes(topCanvas).length, 0);
});

test('the current deck still paints its own tempo curve in the bottom half', () => {
	const { topCanvas, bottomCanvas } = paint(frame(), frame({ tempo: TEMPO }));
	assert.equal(
		tempoStrokes(topCanvas).length,
		0,
		'CONTROL: the top frame has no tempo answer in this paint'
	);
	const strokes = tempoStrokes(bottomCanvas);
	assert.ok(strokes.length > 0, 'the bottom deck frame paints its tempo-colored curve');
	assert.ok(
		strokes.flatMap((stroke) => stroke.points).every(([, y]) => HALF_HEIGHT * 2 - y > HALF_HEIGHT),
		'the flipped bottom curve coordinates stay below the split line'
	);
});
