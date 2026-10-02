import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// NATIVE-22 / specs/ui-contracts/library-preview-waveform: the library row
// preview JIK approved on Fri 2 Oct 2026 ("How preview waveforms should look
// for now: see ss. These look great."). This pins the parts of that image the
// code decides: the strip's size, one-sided bars rising from the baseline,
// the paint order, and the rekordbox 3Band hues on the dark face.
//
// Regression lines:
// - if the default preview stops being blue low / amber mid / white high then broken
// - if a band is painted from the top or mirrored about a centre line then broken
// - if the paint order changes so a later band no longer overdraws an earlier one then broken
// - if the row strip is no longer 165 x 14 CSS px then broken

let strip;
let palette;

before(async () => {
	strip = await loadTypeScriptModule('src/lib/components/rb/deck/strip-waveform-render.ts');
	palette = await loadTypeScriptModule('src/lib/rb/wave-palette.ts');
});

function recordingCtx() {
	const calls = [];
	const ctx = {
		fillStyle: '',
		strokeStyle: '',
		lineWidth: 1,
		globalAlpha: 1,
		fillRect(x, y, w, h) {
			calls.push({ x, y, w, h, fillStyle: ctx.fillStyle });
		},
		beginPath() {},
		moveTo() {},
		lineTo() {},
		stroke() {}
	};
	return { ctx, calls };
}

test('the default preview is rekordbox 3Band: blue low, amber mid, white high', () => {
	const c = palette.resolveStripBandColors('dark');
	assert.equal(c.low, '#2767d8');
	assert.equal(c.mid, 'rgba(240, 160, 32, 0.85)');
	assert.equal(c.high, 'rgba(244, 246, 248, 0.9)');
});

test('bands rise from the baseline, low then mid then high, one column wide', () => {
	const { ctx, calls } = recordingCtx();
	const W = 120;
	const H = 14;
	const bands = { length: 2, low: [1, 0.5], mid: [0.5, 0.25], high: [0.25, 1] };
	strip.drawStripPreviewBands(ctx, bands, 'tri', W, H);
	const c = palette.resolveStripBandColors('dark');
	assert.deepEqual(
		calls.map((r) => r.fillStyle),
		[c.low, c.mid, c.high, c.low, c.mid, c.high]
	);
	for (const r of calls) {
		// Bottom-anchored: every bar's bottom edge is the strip's baseline.
		assert.equal(r.y + r.h, H, `bar not on the baseline: ${JSON.stringify(r)}`);
		assert.equal(r.w, W / bands.length);
	}
	assert.equal(calls[0].h, H);
	assert.equal(calls[5].h, H);
	assert.equal(calls[2].h, 0.25 * H);
});

test('the library row strip is 165 x 14 CSS px', () => {
	const src = readFileSync(
		new URL('../../src/lib/components/rb/browser/PreviewStrip.svelte', import.meta.url),
		'utf8'
	);
	assert.match(src, /const W = 165;/);
	assert.match(src, /const H = 14;/);
});
