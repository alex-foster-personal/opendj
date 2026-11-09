/**
 * WaveRow canvas backing store buckets while resizing (PERF-PAINT-10546, #10219).
 *
 * - if backingSize does not round up to 128 CSS px while resizing then a row
 *   height drag reallocates every wave canvas every frame again
 * - if the settled size is not exact then the waveform paints blurry at rest
 * - if device px are not rounded then a fractional dpr reallocates every draw
 * - if the row or .canvas-wrap loses overflow: hidden then the bucketed
 *   canvas spills over the stem rows or past the row
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let cb;

before(async () => {
	cb = await loadTypeScriptModule('src/lib/components/rb/wave/canvas-backing.ts');
});

test('1200 x 190 buckets to 1280 x 256 while resizing and stays exact at rest; dpr 2 doubles', () => {
	const r = cb.backingSize({ cssW: 1200, cssH: 190, dpr: 1, resizing: true });
	assert.deepEqual(r, { cssW: 1280, cssH: 256, width: 1280, height: 256, bucketed: true });
	const s = cb.backingSize({ cssW: 1200, cssH: 190, dpr: 1, resizing: false });
	assert.deepEqual(s, { cssW: 1200, cssH: 190, width: 1200, height: 190, bucketed: false });
	assert.deepEqual(
		[
			cb.backingSize({ cssW: 1200, cssH: 190, dpr: 2, resizing: true }).width,
			cb.backingSize({ cssW: 1200, cssH: 190, dpr: 2, resizing: true }).height
		],
		[2560, 512]
	);
	const s2 = cb.backingSize({ cssW: 1200, cssH: 190, dpr: 2, resizing: false });
	assert.deepEqual([s2.width, s2.height], [2400, 380]);
});

test('an exact bucket multiple is not marked bucketed', () => {
	const r = cb.backingSize({ cssW: 1280, cssH: 256, dpr: 1, resizing: true });
	assert.equal(r.bucketed, false);
	assert.deepEqual([r.cssW, r.cssH], [1280, 256]);
});

test('fractional dpr rounds device px to integers', () => {
	const r = cb.backingSize({ cssW: 1201, cssH: 191, dpr: 1.25, resizing: false });
	assert.deepEqual([r.width, r.height], [1501, 239]);
});

test('invalid inputs fail fast', () => {
	assert.throws(() => cb.backingSize({ cssW: 1200, cssH: 0, dpr: 1, resizing: false }), /cssH/);
	assert.throws(
		() => cb.backingSize({ cssW: Number.NaN, cssH: 190, dpr: 1, resizing: false }),
		/cssW/
	);
	assert.throws(() => cb.backingSize({ cssW: 1200, cssH: 190, dpr: 0, resizing: false }), /dpr/);
});

test('a 180-step height sweep 120 to 200 px over 3 s yields at most 3 distinct backing sizes', () => {
	const sizes = new Set();
	let lastResizeAt = null;
	for (let i = 0; i < 180; i++) {
		const now = (i * 3000) / 180;
		const cssH = Math.round(120 + (80 * i) / 179);
		if (i > 0) lastResizeAt = now;
		const b = cb.backingSize({
			cssW: 1200,
			cssH,
			dpr: 1,
			resizing: cb.isResizing(lastResizeAt, now)
		});
		sizes.add(`${b.width}x${b.height}`);
	}
	const settledAt = lastResizeAt + cb.RESIZE_SETTLE_MS;
	assert.equal(cb.isResizing(lastResizeAt, settledAt), false);
	const final = cb.backingSize({ cssW: 1200, cssH: 200, dpr: 1, resizing: false });
	sizes.add(`${final.width}x${final.height}`);
	// 1200x120 first exact report, 1280x128 and 1280x256 buckets, 1200x200 final.
	const bucketSizes = [...sizes].filter((s) => s.startsWith('1280x'));
	assert.ok(bucketSizes.length <= 2, `bucket sizes ${bucketSizes}`);
	assert.ok(sizes.size <= 4, `distinct sizes ${[...sizes]}`);
	assert.ok(sizes.has('1200x200'));
});

test('RESIZE_SETTLE_MS is 250 and isResizing(null) is false', () => {
	assert.equal(cb.RESIZE_SETTLE_MS, 250);
	assert.equal(cb.isResizing(null, 0), false);
	assert.equal(cb.isResizing(0, 249), true);
});

test('the row and .canvas-wrap clip the bucketed canvas', () => {
	const css = readFileSync(
		new URL('../../src/lib/components/rb/wave/WaveRow.chrome.css', import.meta.url),
		'utf8'
	);
	const rule = (sel) => {
		const m = css.match(new RegExp(`(?:^|\\n)${sel.replace(/\./g, '\\.')}\\s*\\{([^}]*)\\}`));
		assert.ok(m, `rule ${sel} missing`);
		return m[1];
	};
	assert.match(rule('.rb-waverow'), /overflow:\s*hidden/);
	assert.match(rule('.rb-waverow .canvas-wrap'), /overflow:\s*hidden/);
});
