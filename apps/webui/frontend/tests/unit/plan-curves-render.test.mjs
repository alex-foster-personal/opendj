// requirement: WAVEPLAN-7896f
// [if] a pending plan's outgoing span is painted without bounded edges or in the incoming color [then] this fails, [else stop].
// [if] a pending plan's incoming span is painted without bounded edges or in the outgoing color [then] this fails, [else stop].
// [if] a deck the plan cannot place, or a span off screen, still paints a band [then] this fails, [else stop].
// [if] a skin, monochrome included, recolors the span tokens off red and green [then] this fails, [else stop].
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, describe, it } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

let planRender;

before(async () => {
	planRender = await loadTypeScriptModule('src/lib/components/rb/wave/plan-curves-render.ts');
});

const PALETTE = {
	planFader: '#ffffff',
	planEqLow: '#aaaaaa',
	planEqMid: '#bbbbbb',
	planEqHigh: '#cccccc',
	planFilter: '#dddddd',
	planOut: '#ff3b30',
	planIn: '#35c04f'
};

/** A canvas stand-in that records each filled rect with the fill style and alpha it was painted with. */
function _recordingCtx() {
	const rects = [];
	const ctx = {
		fillStyle: '',
		strokeStyle: '',
		globalAlpha: 1,
		lineWidth: 1,
		fillRect(x, y, w, h) {
			rects.push({ x, y, w, h, color: ctx.fillStyle, alpha: ctx.globalAlpha });
		},
		beginPath() {},
		moveTo() {},
		lineTo() {},
		stroke() {},
		setLineDash() {}
	};
	return { ctx, rects };
}

/** A placed deck overlay with no curves: only its span is under test. */
function _overlay(role, start_ms, end_ms) {
	return {
		deck: role === 'clock' ? 1 : 2,
		role,
		stable_id: 'sid',
		start_ms,
		end_ms,
		curves: [],
		unavailable: start_ms === null ? 'no_grid' : null
	};
}

/** Paint one overlay on a 1000 x 80 row at 100 px per second from t=0, returning the rects drawn. */
function _paint(overlay) {
	const { ctx, rects } = _recordingCtx();
	planRender.drawPlanCurves(ctx, overlay, PALETTE, 0, 100, 1000, 80);
	return rects;
}

describe('WAVEPLAN-7896f: pending plan span bands', () => {
	it('paints the outgoing span as a red tinted band with solid edges at both ends', () => {
		/** [if] the clock deck's span is unbounded or not red [then] the outgoing role is unreadable. */
		const rects = _paint(_overlay('clock', 2000, 6000));
		assert.equal(rects.length, 3);
		assert.ok(rects.every((rect) => rect.color === PALETTE.planOut));
		const [band, startEdge, endEdge] = rects;
		assert.deepEqual([band.x, band.w, band.h], [200, 400, 80]);
		assert.ok(band.alpha < 1, 'the band is a tint, not a solid fill');
		assert.equal(startEdge.alpha, 1);
		assert.equal(endEdge.alpha, 1);
		assert.equal(startEdge.x, 200);
		assert.equal(endEdge.x + endEdge.w, 600);
	});

	it('paints the incoming span in green, never the outgoing color', () => {
		/** [if] the incoming deck's span shares the outgoing color [then] the decks' roles cannot be told apart. */
		const rects = _paint(_overlay('incoming', 1000, 3000));
		assert.equal(rects.length, 3);
		assert.ok(rects.every((rect) => rect.color === PALETTE.planIn));
		assert.equal(rects[1].x, 100);
		assert.equal(rects[2].x + rects[2].w, 300);
	});

	it('paints nothing for a deck the plan cannot place or a span off screen', () => {
		/** [if] an unplaced deck or an off-screen span paints a band [then] the row shows a plan where none can be drawn. */
		assert.deepEqual(_paint(_overlay('clock', null, null)), []);
		assert.deepEqual(_paint(_overlay('incoming', 20_000, 30_000)), []);
	});
});

/** The hue in degrees of a `#rrggbb` color. */
function _hue(hex) {
	const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
	const max = Math.max(r, g, b);
	const delta = max - Math.min(r, g, b);
	if (delta === 0) return null;
	const hue =
		max === r ? ((g - b) / delta) % 6 : max === g ? (b - r) / delta + 2 : (r - g) / delta + 4;
	return (hue * 60 + 360) % 360;
}

describe('WAVEPLAN-7896f: span tokens hold in every skin', () => {
	it('keeps --rb-plan-out red and --rb-plan-in green wherever theme.css sets them', () => {
		/** [if] a skin (mono-dev, a mono wave palette) grays or swaps the span tokens [then] the role colors are lost. */
		const css = readFileSync(
			fileURLToPath(new URL('../../src/lib/rb/theme.css', import.meta.url)),
			'utf8'
		);
		const outs = [...css.matchAll(/--rb-plan-out:\s*(#[0-9a-fA-F]{6})/g)].map((m) => m[1]);
		const ins = [...css.matchAll(/--rb-plan-in:\s*(#[0-9a-fA-F]{6})/g)].map((m) => m[1]);
		assert.ok(outs.length > 0 && ins.length > 0, 'theme.css declares the span tokens');
		for (const out of outs) {
			const hue = _hue(out);
			assert.ok(hue !== null && (hue < 20 || hue > 340), `--rb-plan-out ${out} is red`);
		}
		for (const inn of ins) {
			const hue = _hue(inn);
			assert.ok(hue !== null && hue > 90 && hue < 160, `--rb-plan-in ${inn} is green`);
		}
		assert.doesNotMatch(css, /--rb-plan-(out|in):\s*var\(/, 'span tokens are fixed, not aliased');
	});
});
