/**
 * Issue #4219: the default waveform palette is rekordbox/CDJ 3Band (dark blue
 * LOW, amber MID, white HIGH); the pre-#4219 palette (orange LOW, blue MID,
 * near-white HIGH) stays selectable as 'legacy'.
 *
 * Regression lines:
 * - if low-band energy paints anything but the default blue low color on the
 *   wavestack then the CDJ-parity default is gone (the original bug: our low
 *   band was orange where every CDJ in the room is blue)
 * - if choosing 'legacy' does not paint low orange #e8a13a then the named
 *   alternative is gone
 * - if the loop-cue color follows the lows band (it used to alias it) then
 *   loops turn blue under the new default
 * - if wave-palette.ts drifts from theme.css then the strips and the
 *   wavestack paint different colors for the same band
 * - if the pref does not parse, persist and reject junk then the choice does
 *   not survive reload
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, describe, it } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

const THEME_CSS = readFileSync(
	fileURLToPath(new URL('../../src/lib/rb/theme.css', import.meta.url)),
	'utf8'
);

const SELECTORS = {
	darkBase: '.perf-root',
	lightBase: "html[data-theme='light'] .perf-root",
	darkLegacy: "html[data-wave-palette='legacy'] .perf-root",
	lightLegacy: "html[data-theme='light'][data-wave-palette='legacy'] .perf-root",
	darkMono: "html[data-wave-palette='mono'] .perf-root",
	lightMono: "html[data-theme='light'][data-wave-palette='mono'] .perf-root"
};

let wp;
let cc;
let render;

/** The cascade theme.css produces under .perf-root for (scheme, choice), in
 * the order the blocks win: base, light override, legacy, light legacy. */
function cascade(scheme, choice) {
	const blocks = [SELECTORS.darkBase];
	if (scheme === 'light') blocks.push(SELECTORS.lightBase);
	if (choice === 'legacy') blocks.push(SELECTORS.darkLegacy);
	if (scheme === 'light' && choice === 'legacy') blocks.push(SELECTORS.lightLegacy);
	if (choice === 'mono') blocks.push(SELECTORS.darkMono);
	if (scheme === 'light' && choice === 'mono') blocks.push(SELECTORS.lightMono);
	return Object.assign({}, ...blocks.map((sel) => cc.parseColorTokens(THEME_CSS, sel)));
}

/** Resolve readPalette (the function WaveRow feeds wave/render.ts) against
 * theme.css's real declarations by standing in for getComputedStyle. */
function resolvePalette(scheme, choice) {
	const tokens = cascade(scheme, choice);
	const previous = globalThis.getComputedStyle;
	globalThis.getComputedStyle = () => ({
		getPropertyValue: (name) => tokens[name.replace(/^--/, '')] ?? ''
	});
	try {
		return render.readPalette({});
	} finally {
		globalThis.getComputedStyle = previous;
	}
}

class RecordingPath {
	constructor() {
		this.rects = [];
	}
	rect(x, y, w, h) {
		this.rects.push({ x, y, w, h });
	}
}

/** Recording 2d context: every fill() records the fillStyle and the path. */
function recordingContext(fills) {
	return {
		fillStyle: '',
		strokeStyle: '',
		lineWidth: 1,
		globalAlpha: 1,
		fillRect() {},
		fill(path) {
			fills.push({ style: this.fillStyle, rects: path ? path.rects.length : 0 });
		},
		drawImage() {},
		beginPath() {},
		moveTo() {},
		lineTo() {},
		closePath() {},
		stroke() {}
	};
}

let bandFills = [];

before(async () => {
	globalThis.Path2D = RecordingPath;
	globalThis.document = {
		createElement(tag) {
			assert.equal(tag, 'canvas');
			return { width: 0, height: 0, getContext: () => recordingContext(bandFills) };
		}
	};
	wp = await loadTypeScriptModule('src/lib/rb/wave-palette.ts');
	cc = await loadTypeScriptModule('src/lib/rb/color-contrast.ts');
	render = await loadTypeScriptModule('src/lib/components/rb/wave/render.ts');
});

/** Paint one wavestack frame whose ONLY energy is in the low band and
 * return the colors of the band paths that actually carried rects. */
function paintLowOnly(palette) {
	render.resetWaveBandCacheForTest();
	bandFills = [];
	const n = 1200;
	const detail = { length: n, low: Array(n).fill(0.8), mid: Array(n).fill(0), high: Array(n).fill(0) };
	const anlz = {
		stable_id: 'b'.repeat(40),
		points: n,
		waveform: { kind: 'tri', preview: detail, detail },
		beatgrid: { beat_count: 0, beats: [] },
		cues: [],
		phrases: [],
		vocals: { status: 'not_analyzed' }
	};
	render.drawWaveRow(recordingContext([]), {
		widthCss: 240,
		heightCss: 60,
		positionMs: 30_000,
		durationMs: 120_000,
		anlz,
		palette,
		pitch: 1,
		loop: null
	});
	return bandFills.filter((f) => f.rects > 0).map((f) => f.style);
}

function isBlue(hex) {
	const r = parseInt(hex.slice(1, 3), 16);
	const g = parseInt(hex.slice(3, 5), 16);
	const b = parseInt(hex.slice(5, 7), 16);
	return b > r && b > g;
}

describe('#4219 waveform band palette', () => {
	it('defaults to rekordbox 3Band', () => {
		assert.equal(wp.WAVE_PALETTE_DEFAULT, 'rekordbox');
		assert.equal(wp.parseWavePalette('rekordbox'), 'rekordbox');
		assert.equal(wp.parseWavePalette('legacy'), 'legacy');
		assert.throws(() => wp.parseWavePalette('neon'), /rekordbox\|legacy/);
	});

	it('default: low-band energy paints the dark blue low color on the wavestack', () => {
		const palette = resolvePalette('dark', 'rekordbox');
		assert.equal(palette.low, '#2767d8');
		assert.ok(isBlue(palette.low), `default low ${palette.low} must be blue`);
		const painted = paintLowOnly(palette);
		assert.deepEqual(painted, ['#2767d8'], 'only the low band carries energy, and it is blue');
	});

	it('default: mid is amber and high is white, matching the CDJ 3Band order', () => {
		const palette = resolvePalette('dark', 'rekordbox');
		assert.equal(palette.mid, '#f0a020');
		assert.equal(palette.high, '#f4f6f8');
	});

	it("legacy: low-band energy paints orange #e8a13a, today's pre-#4219 palette", () => {
		const palette = resolvePalette('dark', 'legacy');
		assert.equal(palette.low, '#e8a13a');
		assert.equal(palette.mid, '#3d7dd9');
		assert.equal(palette.high, '#cfe0f2');
		assert.deepEqual(paintLowOnly(palette), ['#e8a13a']);
	});

	it('light theme has its own band hues in both choices, with no near-white high', () => {
		const light = resolvePalette('light', 'rekordbox');
		assert.equal(light.low, '#1d4fa3');
		assert.ok(isBlue(light.low));
		assert.notEqual(light.high, resolvePalette('dark', 'rekordbox').high);
		assert.equal(resolvePalette('light', 'legacy').low, '#a44b11');
	});

	it('loop cues stay orange under both palettes (no longer aliased to the lows band)', () => {
		for (const choice of ['rekordbox', 'legacy']) {
			assert.equal(resolvePalette('dark', choice).cueLoop, '#e8a13a', choice);
		}
	});

	it('mono / line designs keep their own single color, not the amber mids', () => {
		assert.equal(resolvePalette('dark', 'rekordbox').mono, '#3d7dd9');
	});

	it('wave-palette.ts mirrors theme.css for every scheme and choice', () => {
		for (const scheme of ['dark', 'light']) {
			for (const choice of ['rekordbox', 'legacy', 'mono']) {
				const tokens = cascade(scheme, choice);
				assert.deepEqual(
					wp.resolveWaveBandColors(scheme, choice),
					{
						low: tokens['rb-wave-low'],
						mid: tokens['rb-wave-mid'],
						high: tokens['rb-wave-high'],
						mono: tokens['rb-wave-mono'],
						vocal: tokens['rb-wave-vocal']
					},
					`${scheme}/${choice}`
				);
			}
		}
	});

	it('strip colors: legacy dark reproduces the pre-#4219 strip fills exactly', () => {
		assert.deepEqual(wp.resolveStripBandColors('dark', 'legacy'), {
			low: '#e8a13a',
			mid: 'rgba(61, 125, 217, 0.85)',
			high: 'rgba(207, 224, 242, 0.9)',
			mono: '#3d7dd9',
			vocal: '#4fb2ff'
		});
		assert.equal(wp.resolveStripBandColors('dark').low, '#2767d8');
	});

	it('parseWavePalette accepts both choices, passes undefined, rejects junk', () => {
		assert.equal(wp.parseWavePalette('rekordbox'), 'rekordbox');
		assert.equal(wp.parseWavePalette('legacy'), 'legacy');
		assert.equal(wp.parseWavePalette(undefined), undefined);
		assert.throws(() => wp.parseWavePalette('rainbow'), /rekordbox\|legacy/);
	});
});
