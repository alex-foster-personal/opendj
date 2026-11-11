// requirement: CHROME-16
// requirement: WAVEQS-11736
// [if] the wave_grid_lines preview does not paint fewer grid lines for bars than beats_bars, and none for off [then] this fails, [else stop].
// [if] a settings catalog row declares a preview [then] EVERY one of its option values, under EVERY skin, paints visible marks on the preview canvas, [else stop].
// [if] a row that had a preview on origin/main (waveform_design, wave_palette) loses its preview declaration [then] this fails, [else stop].
// [if] SettingsOverlay goes back to a lazy {#await import()} of the preview [then] this fails, since a pending or failed chunk rendered nothing silently, [else stop].
// [if] a waveform design has no painter branch [then] painting it throws instead of falling through to another look, [else stop].
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/** Rows that rendered a preview on origin/main at 11caf6210e (audited Mon 5 Oct 2026). */
const PREVIEW_ROWS_ON_MAIN = ['waveform_design', 'wave_palette'];
/** Which waveform pref each preview row sets; the other one stays on its 'auto' default. */
const PREF_SET_BY_ROW = {
	waveform_design: 'design',
	wave_palette: 'palette',
	wave_height: 'height',
	wave_grid_lines: 'grid'
};
const W = 160;
const H = 36;
/** A preview must cover at least this share of the canvas with non-background marks. */
const MIN_INK_SHARE = 0.1;
/** ...in a color at least this far (sum of RGB channel deltas) from the background. */
const MIN_COLOR_DISTANCE = 60;

let catalog;
let skin;
let palette;
let paint;
let sample;
before(async () => {
	catalog = await loadTypeScriptModule('src/lib/settings/catalog.ts');
	skin = await loadTypeScriptModule('src/lib/rb/ui-skin.ts');
	palette = await loadTypeScriptModule('src/lib/rb/wave-palette.ts');
	globalThis.Path2D = RecordingPath;
	paint = await loadTypeScriptModule('src/lib/settings/waveform-preview-paint.ts');
	sample = (await loadTypeScriptModule('src/lib/settings/waveform-preview-sample.ts')).previewSampleWaveform();
});

function _rgb(color) {
	const hex = color.match(/^#([0-9a-f]{6})$/i);
	if (hex) return [0, 2, 4].map((i) => parseInt(hex[1].slice(i, i + 2), 16)).concat(1);
	const rgba = color.match(/^rgba?\(([^)]+)\)$/);
	if (rgba) {
		const p = rgba[1].split(',').map(Number);
		return [p[0], p[1], p[2], p[3] ?? 1];
	}
	throw new Error(`unparseable preview color ${color}`);
}

function _visibleAgainstBackground(color) {
	const [r, g, b, a] = _rgb(color);
	const [br, bg, bb] = _rgb(paint.PREVIEW_BACKGROUND);
	return a > 0.2 && Math.abs(r - br) + Math.abs(g - bg) + Math.abs(b - bb) >= MIN_COLOR_DISTANCE;
}

/** Path2D stand-in: the preview paints through the deck row's band painter (W15), which builds paths. */
class RecordingPath {
	constructor() {
		this.area = 0;
		this.segments = 0;
	}
	rect(_x, _y, w, h) {
		this.area += Math.max(0, w) * Math.max(0, h);
	}
	moveTo() {}
	lineTo() {
		this.segments += 1;
	}
}

/** A 2D context that records the ink each draw call lays down. */
function recordingContext() {
	const ink = { area: 0, strokeSegments: 0, colors: new Set() };
	const ctx = {
		fillStyle: '',
		strokeStyle: '',
		lineWidth: 1,
		globalAlpha: 1,
		clearRect() {},
		fillRect(_x, _y, w, h) {
			if (_visibleAgainstBackground(ctx.fillStyle)) {
				ink.area += Math.max(0, w) * Math.max(0, h);
				if (w > 0 && h > 0) ink.colors.add(ctx.fillStyle);
			}
		},
		fill(path) {
			if (ctx.globalAlpha > 0.2 && _visibleAgainstBackground(ctx.fillStyle) && path.area > 0) {
				ink.area += path.area;
				ink.colors.add(ctx.fillStyle);
			}
		},
		stroke(path) {
			if (ctx.lineWidth > 0 && _visibleAgainstBackground(ctx.strokeStyle)) ink.strokeSegments += path.segments;
		}
	};
	return { ctx, ink };
}

function previewRows() {
	return catalog.SETTINGS_CATALOG.filter((d) => d.preview !== undefined);
}

test('CHROME-16 if a row that had a preview on main stops declaring one then its thumbnail is gone', () => {
	const ids = previewRows().map((d) => d.id);
	for (const id of PREVIEW_ROWS_ON_MAIN) assert.ok(ids.includes(id), `${id} lost its preview declaration`);
	for (const def of previewRows()) {
		assert.equal(def.preview, 'waveform', `${def.id}: unknown preview kind`);
		assert.equal(def.control.kind, 'enum', `${def.id}: the overlay renders previews under enum controls only`);
		assert.ok(def.id in PREF_SET_BY_ROW, `${def.id}: declares a preview but this test does not know which pref it sets`);
	}
});

test('CHROME-16 if any option value of a preview row paints an empty or invisible preview under any skin then that value has no preview', () => {
	let painted = 0;
	for (const def of previewRows()) {
		for (const { value } of def.control.options) {
			for (const skinId of skin.UI_SKIN_CHOICES) {
				const designPref = PREF_SET_BY_ROW[def.id] === 'design' ? value : 'auto';
				const palettePref = PREF_SET_BY_ROW[def.id] === 'palette' ? value : 'auto';
				const waveHeight = PREF_SET_BY_ROW[def.id] === 'height' ? value : 'full';
				const design = skin.effectiveWaveformDesign(designPref, skinId);
				const choice = skin.effectiveWavePalette(palettePref, skinId);
				const colors = palette.resolveWaveBandColors('dark', choice);
				// Other rows paint grid-off so the band color counts stay band-only.
				const gridLines = PREF_SET_BY_ROW[def.id] === 'grid' ? value : 'off';
				const { ctx, ink } = recordingContext();
				paint.paintWaveformPreview(ctx, W, H, design, colors, sample, waveHeight, gridLines);
				const label = `${def.id}=${value} skin=${skinId} -> ${design}/${choice}`;
				const visible = ink.area >= MIN_INK_SHARE * W * H || ink.strokeSegments >= 40;
				assert.ok(visible, `${label}: preview painted no visible marks (ink area ${ink.area}, stroke segments ${ink.strokeSegments})`);
				if (design === 'tri-band' && gridLines === 'off') assert.equal(ink.colors.size, 3, `${label}: tri-band must show all three bands, saw ${[...ink.colors].join(', ')}`);
				painted += 1;
			}
		}
	}
	// Control: 4 rows x (6 + 5 + 2 + 3 options) x 5 skins. A zero here would mean the loop never ran.
	assert.equal(painted, 80, 'every preview row x option x skin combination was painted');
});

test('CHROME-16 if a design has no painter branch then the preview falls through silently instead of failing', () => {
	const { ctx } = recordingContext();
	const colors = palette.resolveWaveBandColors('dark', 'rekordbox');
	assert.throws(() => paint.paintWaveformPreview(ctx, W, H, 'spiral', colors, sample, 'full', 'off'), /no painter for design spiral/);
});

test('CHROME-16 if SettingsOverlay lazy-imports the preview again then a pending or failed chunk renders nothing', () => {
	// WAVEQS-8348c: the overlay renders previews through the shared SettingPreview,
	// which imports the painter component statically; neither may lazy-import it.
	const src = readFileSync('src/lib/components/settings/SettingsOverlay.svelte', 'utf8');
	const shared = readFileSync('src/lib/components/settings/SettingPreview.svelte', 'utf8');
	assert.match(src, /import SettingPreview from '\.\/SettingPreview\.svelte';/);
	assert.match(src, /\{#if def\.preview !== undefined\}/);
	assert.match(shared, /import WaveformDesignPreview from '\.\/WaveformDesignPreview\.svelte';/);
	for (const file of [src, shared]) {
		assert.doesNotMatch(file, /import\(\s*['"]\.\/(WaveformDesignPreview|SettingPreview)\.svelte['"]\s*\)/);
	}
	assert.doesNotMatch(src, /def\.id === 'waveform_design' \|\| def\.id === 'wave_palette'/, 'previews key on the catalog declaration, not hardcoded ids');
});

test('WAVEQS-11736 the beat grid lines preview shows the three variants', () => {
	const W_WIDE = 640;
	const colors = palette.resolveWaveBandColors('dark', 'rekordbox');
	const gridRects = (gridLines) => {
		let count = 0;
		const { ctx } = recordingContext();
		const fillRect = ctx.fillRect;
		ctx.fillRect = (...args) => {
			if (ctx.fillStyle === paint.PREVIEW_GRID_COLOR) count += 1;
			fillRect(...args);
		};
		paint.paintWaveformPreview(ctx, W_WIDE, H, 'tri-band', colors, sample, 'full', gridLines);
		return count;
	};
	const beatsBars = gridRects('beats_bars');
	const bars = gridRects('bars');
	const off = gridRects('off');
	assert.ok(beatsBars > bars, `beats_bars ${beatsBars} must paint more grid than bars ${bars}`);
	assert.ok(bars > 0, 'bars must paint the downbeat lines');
	assert.equal(off, 0);
});
