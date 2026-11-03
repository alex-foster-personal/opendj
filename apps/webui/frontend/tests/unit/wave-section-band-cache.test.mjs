/**
 * Requirements: STRUCT-05.
 *
 * The section overlay rides in the wavestack's cached band image (PERF-PAINT-01),
 * so an unchanged view pays nothing extra per frame. Regression lines:
 * - if toggling sections on does not rebuild the image then the bands never appear
 * - if every frame rebuilds the image with sections on then the overlay costs a
 *   full-track repaint per frame
 * - if sections paint AFTER the waveform inside the image then they tint it
 * - if a new phrases payload reuses the old image then stale sections render
 * - if a skin changes any one section color (not just drop) and the old image
 *   is reused then stale tints render
 * - CONTROL: sections off bakes no section color into the image
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let render;
let images = [];

function context(log) {
	const ctx = {
		fillStyle: '',
		strokeStyle: '',
		lineWidth: 1,
		globalAlpha: 1,
		font: '',
		textBaseline: 'alphabetic',
		fillRect(x, y, w, h) {
			log.push({ kind: 'fillRect', style: ctx.fillStyle, x, w, h });
		},
		fill(path) {
			log.push({ kind: path === undefined ? 'fill' : 'fillPath', style: ctx.fillStyle });
		},
		fillText(text, x) {
			log.push({ kind: 'fillText', text, x });
		},
		measureText: (text) => ({ width: text.length * 5 }),
		drawImage(canvas) {
			log.push({ kind: 'drawImage', canvas });
		},
		save() {},
		restore() {},
		rect() {},
		clip() {},
		beginPath() {},
		moveTo() {},
		lineTo() {},
		closePath() {},
		stroke() {}
	};
	return ctx;
}

before(async () => {
	globalThis.Path2D = class {
		rect() {}
	};
	globalThis.document = {
		createElement(tag) {
			assert.equal(tag, 'canvas');
			const log = [];
			const canvas = { width: 0, height: 0, log, getContext: () => context(log) };
			images.push(canvas);
			return canvas;
		}
	};
	// One bundle for render.ts and the section painter's lazy loader, so the
	// preload below fills the loader instance drawWaveRow reads.
	render = await loadTypeScriptModule('tests/unit/fixtures/wave-render-sections-entry.ts');
	await render.loadSectionsApi();
});

const SECTIONS = {
	edge: '#00aaff',
	rise: '#ffaa00',
	drop: '#ff0055',
	calm: '#8877cc',
	body: '#6688aa',
	neutral: '#808080',
	text: '#eeeeee'
};
const SECTION_COLORS = new Set(Object.values(SECTIONS));
const PALETTE = {
	bg: '#101010', low: '#e8a13a', mid: '#4f8fff', high: '#f0f0f0', tick: '#ccc',
	cueHotCue: '#35c04f', cueLoop: '#e8a13a', cueMemory: '#ff3b30', cueOutline: '#c8cdd2',
	phrase: '#888', sections: SECTIONS
};

const detail = { length: 1200, low: Array(1200).fill(0.5), mid: Array(1200).fill(0.5), high: Array(1200).fill(0.5) };
const PHRASES = [
	{ start_s: 0, end_s: 30, kind: 0, mood: 0, label: 'intro', source: 'own' },
	{ start_s: 30, end_s: 90, kind: 0, mood: 0, label: 'drop', source: 'own' },
	{ start_s: 90, end_s: 120, kind: 0, mood: 0, label: 'outro', source: 'own' }
];
function anlz(phrases) {
	return {
		stable_id: 'a'.repeat(40), points: 1200, waveform: { kind: 'tri', preview: detail, detail },
		beatgrid: { beat_count: 0, beats: [] }, cues: [], phrases, vocals: { status: 'not_analyzed' }
	};
}

function frame(a, positionMs, showSections) {
	return {
		windowS: 24,
		playheadFrac: 0.5,
		widthCss: 240, heightCss: 60, positionMs, durationMs: 120_000, anlz: a,
		palette: PALETTE, pitch: 1, loop: null, showSections
	};
}

function sectionFills(log) {
	return log.filter((c) => c.kind === 'fillRect' && SECTION_COLORS.has(c.style));
}

test('CONTROL: sections off bakes no section color into the band image', () => {
	render.resetWaveBandCacheForTest();
	images = [];
	const a = anlz(PHRASES);
	render.drawWaveRow(context([]), frame(a, 30_000, false));
	assert.equal(images.length, 1);
	assert.equal(sectionFills(images[0].log).length, 0);
	assert.ok(images[0].log.some((c) => c.kind === 'fillPath'), 'the waveform itself was baked');
});

test('sections on rebuild the image ONCE, under the waveform, then every frame blits it', () => {
	render.resetWaveBandCacheForTest();
	images = [];
	const a = anlz(PHRASES);
	const destLog = [];
	const dest = context(destLog);
	render.drawWaveRow(dest, frame(a, 30_000, false));
	render.drawWaveRow(dest, frame(a, 30_000, true));
	assert.equal(images.length, 2, 'turning sections on must rebuild the band image');
	const log = images[1].log;
	const bodies = sectionFills(log).filter((c) => c.h === 60 && c.w > 1);
	assert.deepEqual(
		bodies.map((c) => c.style),
		[SECTIONS.edge, SECTIONS.drop, SECTIONS.edge],
		'one full-height body per section across the whole track'
	);
	assert.equal(
		log.filter((c) => c.kind === 'fillText').length,
		0,
		'labels are never baked: the CSS-px image is DPR-upscaled, which blurs text'
	);
	const lastSection = log.findLastIndex((c) => SECTION_COLORS.has(c.style));
	const firstWave = log.findIndex((c) => c.kind === 'fillPath');
	assert.ok(lastSection < firstWave, 'sections must be baked under the waveform');

	for (let i = 1; i <= 30; i++) render.drawWaveRow(dest, frame(a, 30_000 + i * 200, true));
	assert.equal(images.length, 2, 'scrolling with sections on must not rebuild the image per frame');
	assert.equal(
		sectionFills(destLog).length,
		0,
		'no section BODY paint lands on the destination canvas per frame'
	);
	assert.ok(
		destLog.filter((c) => c.kind === 'fillText').length >= 30,
		'the labels paint crisp on the destination every frame'
	);

	render.drawWaveRow(dest, frame(a, 40_000, false));
	assert.equal(images.length, 3, 'turning sections off must rebuild without them');
	assert.equal(sectionFills(images[2].log).length, 0);
});

test('a new phrases array for the same waveform rebuilds the image', () => {
	render.resetWaveBandCacheForTest();
	images = [];
	const a = anlz(PHRASES);
	render.drawWaveRow(context([]), frame(a, 30_000, true));
	const b = { ...a, phrases: PHRASES.map((p) => ({ ...p, label: 'breakdown' })) };
	render.drawWaveRow(context([]), frame(b, 30_000, true));
	assert.equal(images.length, 2);
	assert.ok(sectionFills(images[1].log).every((c) => c.style === SECTIONS.calm));
});

test('a change to ANY section color rebuilds the image; the same palette does not', () => {
	const a = anlz(PHRASES);
	const dest = context([]);
	for (const family of ['edge', 'rise', 'drop', 'calm', 'body', 'neutral', 'text']) {
		render.resetWaveBandCacheForTest();
		images = [];
		render.drawWaveRow(dest, frame(a, 30_000, true));
		// CONTROL: an equal palette in a new object reuses the image.
		const same = { ...PALETTE, sections: { ...SECTIONS } };
		render.drawWaveRow(dest, { ...frame(a, 30_000, true), palette: same });
		assert.equal(images.length, 1, 'an unchanged palette must reuse the band image');
		const changed = { ...PALETTE, sections: { ...SECTIONS, [family]: '#123456' } };
		render.drawWaveRow(dest, { ...frame(a, 30_000, true), palette: changed });
		assert.equal(images.length, 2, `changing sections.${family} must rebuild the band image`);
	}
});
