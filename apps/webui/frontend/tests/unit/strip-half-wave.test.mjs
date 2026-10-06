import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// Small waveforms (deck overview strip + library preview strip) are a HALF
// wave: every design rises from the bottom baseline, never mirrored about a
// centerline (the maintainer, Tue 6 Oct 2026: "just top half for the smaller one in deck
// and previews"). The big deck rows are mirrored; see waveform-blocks-design.
// Regression lines:
// - if any strip design paints a bar whose bottom edge is not the baseline then broken
// - if any strip design paints below the baseline or above the strip top then broken
// - if a loud point does not reach the top half (a mirrored painter tops out at the center) then broken
// - if the line design puts a silent point anywhere but the baseline then broken

let strip;

before(async () => {
	globalThis.Path2D = class {
		constructor() {
			this.rects = [];
		}
		rect(x, y, w, h) {
			this.rects.push({ x, y, w, h });
		}
	};
	strip = await loadTypeScriptModule('src/lib/components/rb/deck/strip-waveform-render.ts');
});

function recordingCtx() {
	const rects = [];
	const points = [];
	return {
		rects,
		points,
		fillStyle: '',
		strokeStyle: '',
		lineWidth: 1,
		globalAlpha: 1,
		fillRect(x, y, w, h) {
			rects.push({ x, y, w, h });
		},
		fill(path) {
			for (const r of path.rects) rects.push(r);
		},
		beginPath() {},
		moveTo(x, y) {
			points.push({ x, y });
		},
		lineTo(x, y) {
			points.push({ x, y });
		},
		stroke() {}
	};
}

const W = 120;
const H = 40;
const COLORS = { low: '#111111', mid: '#222222', high: '#333333', mono: '#cfcfcf', vocal: '#00f' };
// Loud first half (one point at full scale), silent second half.
const N = 40;
const loud = Array.from({ length: N }, (_, i) => (i === 0 ? 1 : i < N / 2 ? 0.6 : 0));
const BANDS = { length: N, low: loud, mid: loud.map((v) => v * 0.5), high: loud.map((v) => v * 0.25) };

for (const design of ['tri-band', 'mono', 'blocks']) {
	for (const payload of ['tri', 'mono']) {
		test(`strip ${design} (${payload} payload) is a half wave on the bottom baseline`, () => {
			const ctx = recordingCtx();
			strip.drawStripPreviewBands(ctx, BANDS, payload, W, H, design, COLORS);
			const painted = ctx.rects.filter((r) => r.h > 0);
			assert.ok(painted.length > 0, 'loud half paints bars');
			for (const r of painted) {
				assert.equal(r.y + r.h, H, `bar bottom not on the baseline: ${JSON.stringify(r)}`);
				assert.ok(r.y >= 0, `bar above the strip top: ${JSON.stringify(r)}`);
			}
			const top = Math.min(...painted.map((r) => r.y));
			assert.ok(top < H / 2 - 4, `loudest bar must reach the top half (top y ${top})`);
		});
	}
}

test('strip line design is a half wave: y within the strip, silence on the baseline', () => {
	const ctx = recordingCtx();
	strip.drawStripPreviewBands(ctx, BANDS, 'tri', W, H, 'line', COLORS);
	assert.equal(ctx.points.length, N, 'one vertex per preview point');
	for (const p of ctx.points) assert.ok(p.y >= 0 && p.y <= H, `vertex outside the strip: ${JSON.stringify(p)}`);
	assert.equal(ctx.points[0].y, 0, 'full-scale point touches the strip top');
	for (const p of ctx.points.slice(N / 2)) assert.equal(p.y, H, 'silent point sits on the baseline');
});
