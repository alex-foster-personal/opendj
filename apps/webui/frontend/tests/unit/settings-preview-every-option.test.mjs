// requirement: CHROME-16
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
const PREF_SET_BY_ROW = { waveform_design: 'design', wave_palette: 'palette' };
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
before(async () => {
	catalog = await loadTypeScriptModule('src/lib/settings/catalog.ts');
	skin = await loadTypeScriptModule('src/lib/rb/ui-skin.ts');
	palette = await loadTypeScriptModule('src/lib/rb/wave-palette.ts');
	paint = await loadTypeScriptModule('src/lib/settings/waveform-preview-paint.ts');
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

/** A 2D context that records the ink each draw call lays down. */
function recordingContext() {
	const ink = { area: 0, strokeSegments: 0, colors: new Set() };
	let pathSegments = 0;
	const ctx = {
		fillStyle: '',
		strokeStyle: '',
		lineWidth: 1,
		clearRect() {},
		fillRect(_x, _y, w, h) {
			if (_visibleAgainstBackground(ctx.fillStyle)) {
				ink.area += Math.max(0, w) * Math.max(0, h);
				if (w > 0 && h > 0) ink.colors.add(ctx.fillStyle);
			}
		},
		beginPath() {
			pathSegments = 0;
		},
		moveTo() {},
		lineTo() {
			pathSegments += 1;
		},
		stroke() {
			if (ctx.lineWidth > 0 && _visibleAgainstBackground(ctx.strokeStyle)) ink.strokeSegments += pathSegments;
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
				const design = skin.effectiveWaveformDesign(designPref, skinId);
				const choice = skin.effectiveWavePalette(palettePref, skinId);
				const colors = palette.resolveStripBandColors('dark', choice);
				const { ctx, ink } = recordingContext();
				paint.paintWaveformPreview(ctx, W, H, design, colors);
				const label = `${def.id}=${value} skin=${skinId} -> ${design}/${choice}`;
				const visible = ink.area >= MIN_INK_SHARE * W * H || ink.strokeSegments >= 40;
				assert.ok(visible, `${label}: preview painted no visible marks (ink area ${ink.area}, stroke segments ${ink.strokeSegments})`);
				if (design === 'tri-band') assert.equal(ink.colors.size, 3, `${label}: tri-band must show all three bands, saw ${[...ink.colors].join(', ')}`);
				painted += 1;
			}
		}
	}
	// Control: 2 rows x (5 + 4 options) x 3 skins. A zero here would mean the loop never ran.
	assert.equal(painted, 27, 'every preview row x option x skin combination was painted');
});

test('CHROME-16 if a design has no painter branch then the preview falls through silently instead of failing', () => {
	const { ctx } = recordingContext();
	const colors = palette.resolveStripBandColors('dark', 'rekordbox');
	assert.throws(() => paint.paintWaveformPreview(ctx, W, H, 'spiral', colors), /no painter for design spiral/);
});

test('CHROME-16 if SettingsOverlay lazy-imports the preview again then a pending or failed chunk renders nothing', () => {
	const src = readFileSync('src/lib/components/settings/SettingsOverlay.svelte', 'utf8');
	assert.match(src, /import WaveformDesignPreview from '\.\/WaveformDesignPreview\.svelte';/);
	assert.match(src, /\{#if def\.preview === 'waveform'\}/);
	assert.doesNotMatch(src, /import\(\s*['"]\.\/WaveformDesignPreview\.svelte['"]\s*\)/);
	assert.doesNotMatch(src, /def\.id === 'waveform_design' \|\| def\.id === 'wave_palette'/, 'previews key on the catalog declaration, not hardcoded ids');
});
