import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// TEMPOROW-8874c - the row strip's tempo curve must be visible at 14 px.
// [if] a two-segment track (10% slower then 10% faster) is drawn on the row strip
// [then] its colored strokes are at least 1 px wide and span most of the band,
// and a steady answer draws nothing, [else stop].

let rowStrip;
let marks;

before(async () => {
	globalThis.Path2D = class {
		constructor(d) {
			this.d = d;
		}
	};
	rowStrip = await loadTypeScriptModule('src/lib/components/rb/browser/row-strip-tempo.ts');
	marks = await loadTypeScriptModule('src/lib/components/rb/wave/tempo-marks.ts');
});

function recordingCtx() {
	const strokes = [];
	let path = [];
	const ctx = {
		fillStyle: '',
		strokeStyle: '',
		lineWidth: 1,
		lineJoin: '',
		lineCap: '',
		setLineDash() {},
		save() {},
		restore() {},
		translate() {},
		scale() {},
		closePath() {},
		fillRect() {},
		fill() {},
		beginPath() {
			path = [];
		},
		moveTo(x, y) {
			path.push([x, y]);
		},
		lineTo(x, y) {
			path.push([x, y]);
		},
		stroke(p) {
			if (p === undefined) strokes.push({ points: path, color: ctx.strokeStyle, width: ctx.lineWidth });
		}
	};
	return { ctx, strokes };
}

const COLORS = () => marks.TEMPO_LANE_COLORS.dark;
const seg = (start_s, end_s, bpm, direction) => ({
	start_s,
	end_s,
	bpm_start: bpm,
	bpm_end: bpm,
	kind: 'jump',
	severity: 0.7,
	direction
});

test('a 10 percent slower then faster track spans the band with a 1 px line', () => {
	const { ctx, strokes } = recordingCtx();
	const curve = {
		nominal_bpm: 120,
		largest_change_pct: 10,
		segments: [seg(30, 90, 108, 'slower'), seg(120, 180, 132, 'faster')]
	};
	rowStrip.drawRowStripTempo(ctx, curve, 240_000, 165, 14, 0.7, COLORS());
	const ink = strokes.filter((s) => Object.values(COLORS()).includes(s.color));
	assert.ok(ink.length >= 2, 'both segments inked');
	for (const s of ink) assert.ok(s.width >= 1, `stroke ${s.width}px is too thin to see`);
	const ys = ink.flatMap((s) => s.points.map(([, y]) => y));
	const band = rowStrip.rowStripTempoBand(14, 0.7);
	const span = Math.max(...ys) - Math.min(...ys);
	assert.ok(span >= band.height * 0.9, `curve spans ${span}px of a ${band.height}px band`);
	assert.ok(Math.min(...ys) >= band.top - 1e-9 && Math.max(...ys) <= band.top + band.height + 1e-9);
});

test('control: a steady answer draws no stroke', () => {
	const { ctx, strokes } = recordingCtx();
	rowStrip.drawRowStripTempo(ctx, { nominal_bpm: 120, largest_change_pct: 0, segments: [] }, 240_000, 165, 14, 0.7, COLORS());
	assert.equal(strokes.length, 0);
});
