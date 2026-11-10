import assert from 'node:assert/strict';
import test from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// Pure maths behind sampleUnmeasuredBackdrops (#10368): grid and edge
// samples, worst-ratio reduction, and the cases that stay unmeasured.
const rc = await loadTypeScriptModule('tests/e2e/support/rendered-contrast-pixels.ts');

/** A `width` x `height` RGBA image whose pixel (x, y) is `paint(x, y)` ([r, g, b] or [r, g, b, a]). */
function image(width, height, paint) {
	const data = new Uint8Array(width * height * 4);
	for (let y = 0; y < height; y++) {
		for (let x = 0; x < width; x++) {
			const [r, g, b, a = 255] = paint(x, y);
			data.set([r, g, b, a], (y * width + x) * 4);
		}
	}
	return { width, height, data };
}

const round = (v) => Math.round(v * 100) / 100;
const full = (img) => ({ x: 0, y: 0, width: img.width, height: img.height });

test('backdropSamplePoints is a 3 by 3 grid plus the four edge midpoints', () => {
	const pts = rc.backdropSamplePoints(30, 12);
	const keys = new Set(pts.map((p) => `${p.x},${p.y}`));
	assert.equal(pts.length, 13, 'if a 30x12 box does not give 13 unique points then broken');
	for (const x of [5, 15, 25])
		for (const y of [2, 6, 10])
			assert.ok(keys.has(`${x},${y}`), `if grid point ${x},${y} is missing then broken`);
	for (const k of ['0,6', '29,6', '15,0', '15,11'])
		assert.ok(keys.has(k), `if edge point ${k} is missing then broken`);
	assert.deepEqual(
		rc.backdropSamplePoints(1, 1),
		[{ x: 0, y: 0 }],
		'if a 1x1 box does not give one point then broken'
	);
	for (const p of rc.backdropSamplePoints(2, 3)) {
		assert.ok(
			p.x >= 0 && p.x < 2 && p.y >= 0 && p.y < 3,
			'if a sample point falls outside the box then broken'
		);
	}
});

test('worstBackdropRatio finds the light and dark ends of a gradient at its edge samples', () => {
	const grad = image(40, 20, (_x, y) => {
		const v = Math.round(255 * (1 - y / 19));
		return [v, v, v];
	});
	const white = rc.worstBackdropRatio(grad, full(grad), [255, 255, 255], 'pixel');
	assert.ok(
		white.ratio < 1.01,
		'if white text over a white-to-black gradient is not ~1:1 then broken'
	);
	assert.deepEqual(
		white.bg,
		[255, 255, 255],
		'if the top edge sample is not the worst for white then broken'
	);
	const black = rc.worstBackdropRatio(grad, full(grad), [0, 0, 0], 'pixel');
	assert.ok(black.ratio < 1.01, 'if black text over the gradient is not ~1:1 then broken');
	assert.deepEqual(
		black.bg,
		[0, 0, 0],
		'if the bottom edge sample is not the worst for black then broken'
	);
	const dark = image(40, 20, () => [0, 0, 0]);
	assert.ok(
		rc.worstBackdropRatio(dark, full(dark), [0x99, 0x99, 0x99], 'pixel').ratio > 4.5,
		'if mid grey on black fails then broken'
	);
});

test('worstBackdropRatio reduces to the planted 1.26:1 on a flat fill in both modes', () => {
	const flat = image(20, 10, () => [0x88, 0x88, 0x88]);
	for (const via of ['pixel', 'svg']) {
		const r = rc.worstBackdropRatio(flat, full(flat), [0x77, 0x77, 0x77], via);
		assert.equal(round(r.ratio), 1.26, `if #777 on #888 is not 1.26:1 in ${via} mode then broken`);
	}
});

test('svg mode reads every pixel, pixel mode only the grid and edges', () => {
	const img = image(30, 12, (x, y) => (x === 3 && y === 4 ? [0, 0, 0] : [255, 255, 255]));
	assert.equal(
		round(rc.worstBackdropRatio(img, full(img), [0, 0, 0], 'svg').ratio),
		1,
		'if svg mode misses a lone clashing pixel then broken'
	);
	assert.equal(
		round(rc.worstBackdropRatio(img, full(img), [0, 0, 0], 'pixel').ratio),
		21,
		'if pixel mode reads off-grid pixels then broken'
	);
});

test('worstBackdropRatio leaves translucent, zero-sized and off-screen boxes unmeasured', () => {
	const glass = image(10, 10, () => [255, 255, 255, 128]);
	assert.deepEqual(
		rc.worstBackdropRatio(glass, full(glass), [0, 0, 0], 'pixel'),
		{ why: 'translucent pixels' },
		'if translucent pixels are measured then broken'
	);
	const img = image(10, 10, (x) => (x < 5 ? [0, 0, 0] : [255, 255, 255]));
	assert.deepEqual(
		rc.worstBackdropRatio(img, { x: 0, y: 0, width: 0, height: 5 }, [0, 0, 0], 'pixel'),
		{ why: 'zero-sized' },
		'if a zero-width box is measured then broken'
	);
	assert.deepEqual(
		rc.worstBackdropRatio(img, { x: 20, y: 0, width: 5, height: 5 }, [0, 0, 0], 'pixel'),
		{ why: 'off-screen' },
		'if an off-image box is measured then broken'
	);
	const half = rc.worstBackdropRatio(
		img,
		{ x: -5, y: 0, width: 10, height: 10 },
		[0, 0, 0],
		'pixel'
	);
	assert.equal(
		round(half.ratio),
		1,
		'if a half-visible box does not sample its visible dark half then broken'
	);
});

test('requiredRatio and contrastRatio follow WCAG 1.4.3', () => {
	assert.equal(
		rc.requiredRatio(16, false),
		4.5,
		'if 16px regular text does not need 4.5:1 then broken'
	);
	assert.equal(rc.requiredRatio(24, false), 3, 'if 24px text does not need 3:1 then broken');
	assert.equal(rc.requiredRatio(19, true), 3, 'if 19px bold text does not need 3:1 then broken');
	assert.equal(
		round(rc.contrastRatio([0, 0, 0], [255, 255, 255])),
		21,
		'if black on white is not 21:1 then broken'
	);
});
