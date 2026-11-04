/**
 * F08 / GB-6840 (#7147): the deck-strip "native grid" marker must be readable
 * AND measurable over a real waveform canvas.
 *
 * Regression lines:
 * - if the label goes back to bare green text on a transparent span over the
 *   canvas, the rendered contrast audit reports `span.native-grid` unmeasured
 *   (24 samples: 4 skin/theme states x 3 performance surfaces) and the root
 *   suite fails with no ratio to measure
 * - if the pill loses its opaque fill, the audit silently loses its backdrop again
 * - if the fill/text pair drifts below the WCAG AA body floor on any shipped
 *   palette, the marker is unreadable on that skin
 *
 * The pair asserted here is the palette-validated one also used by `.cue-letter`
 * (color-contrast.ts: fg rb-bg on bg rb-green, body floor 4.5:1).
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, describe, it } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

const STRIP_WAVEFORM = fileURLToPath(
	new URL('../../src/lib/components/rb/deck/StripWaveform.svelte', import.meta.url)
);
const THEME_CSS = readFileSync(fileURLToPath(new URL('../../src/lib/rb/theme.css', import.meta.url)), 'utf8');

const PALETTES = [
	['default dark', '.perf-root'],
	['default light', "html[data-theme='light'] .perf-root"],
	['mono-dev dark', "html[data-skin='mono-dev'] .perf-root"],
	['mono-dev light', "html[data-skin='light'] .perf-root"]
];

let cuesMod;
let ccMod;

before(async () => {
	cuesMod = await loadTypeScriptModule('src/lib/components/rb/wave/cues.ts');
	ccMod = await loadTypeScriptModule('src/lib/rb/color-contrast.ts');
});

function expandShortHex(color) {
	const m = /^#([0-9a-f]{3})$/i.exec(color.trim());
	if (m === null) return color.trim();
	const [r, g, b] = m[1].split('');
	return `#${r}${r}${g}${g}${b}${b}`;
}

function resolveCssColor(value, tokens) {
	const trimmed = value.trim();
	const varMatch = /^var\(--([a-z0-9-]+)\)$/i.exec(trimmed);
	if (varMatch !== null) {
		const hex = tokens[varMatch[1]];
		assert.ok(hex, `missing theme token --${varMatch[1]}`);
		return hex;
	}
	return expandShortHex(trimmed);
}

function cssBlock(selector) {
	const blockMatch = new RegExp(`${selector.replace(/[.[\]'=]/g, '\\$&')}\\s*\\{([^}]*)\\}`).exec(
		readFileSync(STRIP_WAVEFORM, 'utf8')
	);
	assert.ok(blockMatch, `StripWaveform.svelte must declare ${selector} styles`);
	return blockMatch[1];
}

function declaration(block, prop) {
	const m = new RegExp(`(?:^|[\\s;])${prop}:\\s*([^;]+)`).exec(block);
	assert.ok(m, `${prop} must be declared`);
	return m[1].trim();
}

describe('deck-strip native grid marker contrast (F08 / GB-6840)', () => {
	it('keeps the marker text and test id the unit contract pins', () => {
		const source = readFileSync(STRIP_WAVEFORM, 'utf8');
		assert.match(source, /data-testid="native-grid-marker"/);
		assert.match(source, />native grid</);
	});

	it('.native-grid paints text on an opaque theme fill', () => {
		const block = cssBlock('.native-grid');
		assert.equal(declaration(block, 'background'), 'var(--rb-green)');
		assert.equal(declaration(block, 'color'), 'var(--rb-bg)');
	});

	it('.native-grid-wrap keeps the centring the old full-bleed span provided', () => {
		const block = cssBlock('.native-grid-wrap');
		assert.equal(declaration(block, 'position'), 'absolute');
		assert.equal(declaration(block, 'inset'), '0');
		assert.equal(declaration(block, 'display'), 'flex');
		assert.equal(declaration(block, 'pointer-events'), 'none');
	});

	it('clears the WCAG AA body floor on all four skin/theme states', () => {
		const styles = cssBlock('.native-grid');
		for (const [name, selector] of PALETTES) {
			const tokens = ccMod.parseColorTokens(THEME_CSS, selector);
			const fg = resolveCssColor(declaration(styles, 'color'), tokens);
			const bg = resolveCssColor(declaration(styles, 'background'), tokens);
			const ratio = cuesMod.contrastRatio(fg, bg);
			assert.ok(
				ratio >= cuesMod.CUE_MIN_CONTRAST,
				`${name}: native grid marker only clears ${ratio.toFixed(3)}:1, below ${cuesMod.CUE_MIN_CONTRAST}:1`
			);
		}
	});
});
