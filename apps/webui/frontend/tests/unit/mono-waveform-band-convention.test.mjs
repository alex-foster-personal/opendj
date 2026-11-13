import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// Browser half of the mono waveform band convention (issue #12585). The backend
// serves a heights-only lane as kind 'mono' with one envelope under low/mid/high
// (tests/webui/test_mono_waveform_band_convention.py pins that against the same
// fixture); the painter takes the per-point max across the three bands and fills
// it once in the mono colour.
// Regression lines:
// - if a mono payload paints any low, mid or high band colour then broken
// - if a mono point is not drawn at the per-point max of low, mid and high then broken
// - if moving a mono envelope between band keys changes the painted shape then broken

const H = 40;
const PALETTE = { low: '#low', mid: '#mid', high: '#high', mono: '#mono' };

let render;
let fixture;

before(async () => {
	globalThis.Path2D = class {
		constructor() {
			this.rects = [];
		}
		rect(x, y, w, h) {
			this.rects.push({ x, y, w, h });
		}
	};
	render = await loadTypeScriptModule('src/lib/components/rb/wave/render.ts');
	fixture = JSON.parse(
		readFileSync(new URL('./fixtures/mono-waveform-band-convention.json', import.meta.url), 'utf8')
	);
});

/** Paint `detail` as a kind-mono waveform, one column per point; return the fills by colour. */
function paint(detail, design = 'tri-band') {
	const fills = new Map();
	const ctx = {
		fillStyle: '',
		strokeStyle: '',
		globalAlpha: 1,
		lineWidth: 1,
		fill(path) {
			const list = fills.get(this.fillStyle) ?? [];
			list.push(...path.rects);
			fills.set(this.fillStyle, list);
		},
		fillRect() {},
		stroke() {}
	};
	const waveform = { kind: 'mono', preview: detail, detail };
	const w = detail.length;
	render.__test_drawBands(ctx, waveform, 1, w, w, H, PALETTE, design, undefined, 0);
	return fills;
}

/** The fixture's backend mono detail block. */
function fixtureDetail() {
	return fixture.waveform.detail;
}

for (const design of ['tri-band', 'mono']) {
	test(`a mono payload paints only the mono colour, one bar per non-silent point (${design})`, () => {
		const d = fixtureDetail();
		const fills = paint(d, design);
		assert.deepEqual(
			[...fills.keys()].filter((k) => k !== PALETTE.mono),
			[],
			'no band colour is painted'
		);
		const bars = fills.get(PALETTE.mono);
		assert.deepEqual(
			bars.map((r) => r.x),
			d.low.flatMap((v, i) => (v > 0 ? [i] : [])),
			'silent points draw nothing, every other point draws one bar'
		);
		const byX = new Map(bars.map((r) => [r.x, r.h]));
		assert.equal(byX.get(1), byX.get(7), 'equal values draw equal bars');
		assert.ok(byX.get(1) > byX.get(4) && byX.get(4) > byX.get(2), 'bar height follows the value');
	});
}

test('a mono point is drawn at the per-point max of low, mid and high', () => {
	const d = fixtureDetail();
	const expected = paint(d).get(PALETTE.mono);
	const zero = d.low.map(() => 0);
	const half = d.low.map((v) => v / 2);
	const variants = {
		'envelope in low only': { low: d.low, mid: zero, high: zero },
		'envelope in mid only': { low: zero, mid: d.low, high: zero },
		'envelope in high only, a quieter copy in low': { low: half, mid: zero, high: d.low },
		'envelope split across low and high by point': {
			low: d.low.map((v, i) => (i < 4 ? v : 0)),
			mid: half,
			high: d.low.map((v, i) => (i < 4 ? 0 : v))
		}
	};
	for (const [name, bands] of Object.entries(variants)) {
		const got = paint({ ...bands, length: d.length }).get(PALETTE.mono);
		assert.deepEqual(got, expected, name);
	}
});
