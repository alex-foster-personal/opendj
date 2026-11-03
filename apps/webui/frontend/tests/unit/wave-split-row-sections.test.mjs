/**
 * Requirements: STRUCT-05.
 *
 * A split row paints two one-sided halves; the bottom one is flipped, so its
 * section names would read upside down and are dropped while its band tints
 * stay. Regression lines:
 * - if the bottom half paints section names then they render upside down
 * - CONTROL: if the top half loses its names then the overlay goes unlabeled
 * - if the halves stop getting a fresh frame copy each (one spread, as before
 *   sections) and the caller's frames are mutated then the next paint inherits
 *   the label flag
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;
const canvases = [];

function recording() {
	const texts = [];
	const fills = [];
	const ctx = new Proxy(
		{
			fillStyle: '',
			globalAlpha: 1,
			measureText: (t) => ({ width: t.length * 5 }),
			fillText(text) {
				texts.push(text);
			},
			fillRect() {
				fills.push(ctx.fillStyle);
			}
		},
		{ get: (t, k) => (k in t ? t[k] : () => {}), set: (t, k, v) => ((t[k] = v), true) }
	);
	return { ctx, texts, fills };
}

before(async () => {
	globalThis.Path2D ??= class {
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
	await mod.loadSectionsApi();
});

const SECTIONS = { edge: '#00aaff', rise: '#ffaa00', drop: '#ff0055', calm: '#8877cc', body: '#6688aa', neutral: '#808080', text: '#eeeeee' };
const PALETTE = {
	bg: '#101010', low: '#e8a13a', mid: '#4f8fff', high: '#f0f0f0', tick: '#ccc',
	cueHotCue: '#35c04f', cueLoop: '#e8a13a', cueMemory: '#ff3b30', cueOutline: '#c8cdd2',
	phrase: '#888', sections: SECTIONS
};
const detail = { length: 64, low: Array(64).fill(0.5), mid: Array(64).fill(0.5), high: Array(64).fill(0.5) };
const ANLZ = {
	stable_id: 'a'.repeat(40), points: 64, waveform: { kind: 'tri', preview: detail, detail },
	beatgrid: { beat_count: 0, beats: [] }, cues: [], vocals: { status: 'not_analyzed' },
	phrases: [
		{ start_s: 0, end_s: 30, kind: 0, mood: 0, label: 'intro', source: 'own' },
		{ start_s: 30, end_s: 90, kind: 0, mood: 0, label: 'drop', source: 'own' }
	]
};

test('the flipped bottom half keeps band tints and drops section names; the top keeps both', () => {
	const half = () => ({
		windowS: 24,
		playheadFrac: 0.5,
		widthCss: 240, heightCss: 30, positionMs: 30_000, durationMs: 120_000, anlz: ANLZ,
		palette: PALETTE, pitch: 1, loop: null, showSections: true
	});
	const top = half();
	const bottom = half();
	const row = {};
	const target = recording();
	const first = canvases.length;
	mod.paintSplitRow(row, target.ctx, top, bottom, { top: 'A', bottom: 'B', color: '#fff', line: '#888' }, 1);
	// The two scratch halves are created first; later canvases are the
	// cached band images drawWaveRow bakes (which hold the band tints).
	const [topCanvas, bottomCanvas] = canvases.slice(first, first + 2);
	const sectionColors = new Set(Object.values(SECTIONS));
	assert.ok(topCanvas.rec.texts.length > 0, 'CONTROL: the top half names its sections');
	// Both halves share one waveform here, so one cached band image serves
	// both; the bottom half blits it like the top.
	const bandImages = canvases.slice(first + 2);
	assert.ok(bandImages.length >= 1);
	assert.ok(bandImages.every((c) => c.rec.fills.some((s) => sectionColors.has(s))), 'the band image carries the tints');
	assert.deepEqual(bottomCanvas.rec.texts, [], 'the flipped half paints no section names');
	assert.equal('sectionLabels' in bottom, false, "the caller's frame is not mutated");
});
