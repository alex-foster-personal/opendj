// requirement: DECKUX-02
/**
 * the maintainer, Mon 5 Oct 2026: "the radial waveform needs same color scheme as the
 * main ones". The jog-wheel radial painter used to carry its own hard-coded
 * legacy palette (orange low, blue mid), so it ignored the 3Band default, the
 * skin and Settings > Waveform colors that the deck rows honour.
 *
 * Regression lines:
 * - if the radial painter fills a band with any colour/alpha the main row
 *   painter would not use for the same palette then the two schemes diverged
 * - if jog-radial-render.ts contains a colour literal then a duplicated
 *   palette has crept back in
 * - if JogDial stops reading readPalette() or stops re-reading it on a
 *   theme / wave_palette / skin swap then the wheel stops following them
 * - if JogDial ignores waveform_design then a mono design leaves the wheel tri
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const SRC = fileURLToPath(new URL('../../src', import.meta.url));
const jogDialSource = readFileSync(`${SRC}/lib/components/rb/deck/JogDial.svelte`, 'utf8');
const radialSource = readFileSync(`${SRC}/lib/components/rb/deck/jog-radial-render.ts`, 'utf8');

let radial;
let render;
let wavePalette;

before(async () => {
	globalThis.Path2D = class {
		constructor() {
			this.rects = [];
		}
		rect(x, y, w, h) {
			this.rects.push({ x, y, w, h });
		}
	};
	radial = await loadTypeScriptModule('src/lib/components/rb/deck/jog-radial-render.ts');
	render = await loadTypeScriptModule('src/lib/components/rb/wave/render.ts');
	wavePalette = await loadTypeScriptModule('src/lib/rb/wave-palette.ts');
});

/** Records every fill as "<colour>@<alpha>"; geometry calls are no-ops. */
function recordingCtx() {
	const fills = new Set();
	return {
		fills,
		fillStyle: '',
		strokeStyle: '',
		lineWidth: 1,
		globalAlpha: 1,
		clearRect() {},
		beginPath() {},
		closePath() {},
		arc() {},
		fillRect() {
			fills.add(`${this.fillStyle}@${this.globalAlpha}`);
		},
		fill() {
			fills.add(`${this.fillStyle}@${this.globalAlpha}`);
		},
		stroke() {}
	};
}

const N = 64;
const ramp = (scale) => Array.from({ length: N }, (_, i) => scale * (0.2 + (0.8 * i) / N));
const BANDS = { length: N, low: ramp(1), mid: ramp(0.7), high: ramp(0.5) };

function radialFills(palette, kind) {
	const ctx = recordingCtx();
	radial.paintJogRadial(ctx, {
		widthPx: 104,
		heightPx: 104,
		palette,
		kind,
		preview: BANDS,
		vocals: null,
		durationSec: null
	});
	return [...ctx.fills].sort();
}

function mainFills(palette, design) {
	const ctx = recordingCtx();
	const full = { ...palette, bg: '#000000', secondaryBg: '#000000' };
	render.__test_drawBands(ctx, { detail: BANDS, kind: 'tri' }, 400 / 24, 24, 400, 80, full, design);
	return [...ctx.fills].sort();
}

function everyPalette() {
	const out = [];
	for (const [scheme, choices] of Object.entries(wavePalette.WAVE_BAND_COLORS)) {
		for (const [choice, colors] of Object.entries(choices)) {
			out.push({ label: `${scheme}/${choice}`, palette: { ...colors } });
		}
	}
	return out;
}

test('[if] radial tri bands differ from main row fills [then] fail, [else stop].', () => {
	const cases = everyPalette();
	assert.ok(cases.length >= 6, 'expected dark+light x rekordbox/legacy/mono palettes');
	for (const { label, palette } of cases) {
		const main = mainFills(palette, 'tri-band');
		assert.equal(main.length, 3, `${label}: main row paints exactly low, mid and high`);
		assert.deepEqual(radialFills(palette, 'tri'), main, `${label}: radial tri bands must match the deck rows`);
	}
});

test('[if] radial mono fill differs from main mono design [then] fail, [else stop].', () => {
	for (const { label, palette } of everyPalette()) {
		assert.deepEqual(radialFills(palette, 'mono'), mainFills(palette, 'mono'), `${label}: mono colour`);
	}
});

test('[if] radial vocal arcs ignore palette.vocal [then] fail, [else stop].', () => {
	const palette = { low: '#101010', mid: '#202020', high: '#303030', mono: '#404040', vocal: '#abcdef' };
	const ctx = recordingCtx();
	radial.paintJogRadial(ctx, {
		widthPx: 104,
		heightPx: 104,
		palette,
		kind: 'tri',
		preview: BANDS,
		vocals: { status: 'rekordbox', regions: [{ start_s: 0, end_s: 30, intensity: 3 }] },
		durationSec: 120
	});
	assert.ok([...ctx.fills].some((f) => f.startsWith('#abcdef@')), 'vocal arcs paint palette.vocal');
});

test('[if] radial painter holds a colour literal [then] fail, [else stop].', () => {
	const code = radialSource.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '');
	assert.doesNotMatch(code, /#[0-9a-f]{3,8}\b/i, 'no hex colour literals in jog-radial-render.ts');
	assert.doesNotMatch(code, /\brgba?\(/i, 'no rgb() colour literals in jog-radial-render.ts');
	assert.doesNotMatch(code, /VOCAL_BLUE/, 'vocal colour comes from the palette, not a constant');
});

test('[if] JogDial skips readPalette or a pref swap [then] fail, [else stop].', () => {
	assert.match(jogDialSource, /readPalette\(c\)/, 'JogDial reads the .perf-root CSS-var palette');
	const effect = jogDialSource.match(/\$effect\(\(\) => \{[\s\S]*?readPalette\(c\)[\s\S]*?\}\);/);
	assert.ok(effect, 'palette is read inside an effect');
	for (const pref of ['theme', 'wave_palette', 'ui_skin']) {
		assert.match(effect[0], new RegExp(`uiPrefs\\.${pref}`), `palette re-reads on ${pref}`);
	}
	assert.match(
		jogDialSource,
		/resolveStripWaveformKind\(deck\.anlz\.waveform\.kind, uiPrefs\.waveform_design\)/,
		'the wheel honours the mono waveform design like the deck rows'
	);
});
