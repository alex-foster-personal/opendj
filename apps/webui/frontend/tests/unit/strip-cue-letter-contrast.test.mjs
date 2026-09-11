/**
 * Deck-strip hotcue letter chips (.cue-letter in StripWaveform.svelte) must
 * clear the same WCAG AA body floor as main-waveform cue markers (issue #2072).
 *
 * Regression lines:
 * - if white-on-green returns on .cue-letter then dark-theme chips are unreadable
 * - if palette tokens change without re-measuring then strip chips can silently fail
 * - if CUE_MIN_CONTRAST drifts from the main waveform policy then the two surfaces diverge
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, describe, it } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

const STRIP_WAVEFORM = fileURLToPath(
	new URL('../../src/lib/components/rb/deck/StripWaveform.svelte', import.meta.url)
);
const THEME_CSS = readFileSync(
	fileURLToPath(new URL('../../src/lib/rb/theme.css', import.meta.url)),
	'utf8'
);

const DARK_SELECTOR = '.perf-root';
const LIGHT_SELECTOR = "html[data-theme='light'] .perf-root";

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

function parseCueLetterStyles(source) {
	const blockMatch = /\.cue-letter\s*\{([^}]*)\}/.exec(source);
	assert.ok(blockMatch, 'StripWaveform.svelte must declare .cue-letter styles');
	const block = blockMatch[1];
	const colorMatch = /(?:^|[\s;])color:\s*([^;]+)/.exec(block);
	const backgroundMatch = /(?:^|[\s;])background:\s*([^;]+)/.exec(block);
	assert.ok(colorMatch, '.cue-letter must declare color');
	assert.ok(backgroundMatch, '.cue-letter must declare background');
	return {
		color: colorMatch[1].trim(),
		background: backgroundMatch[1].trim()
	};
}

function cueLetterContrast(selector) {
	const tokens = ccMod.parseColorTokens(THEME_CSS, selector);
	const styles = parseCueLetterStyles(readFileSync(STRIP_WAVEFORM, 'utf8'));
	const fg = resolveCssColor(styles.color, tokens);
	const bg = resolveCssColor(styles.background, tokens);
	return {
		ratio: cuesMod.contrastRatio(fg, bg),
		styles,
		fg,
		bg
	};
}

describe('deck-strip hotcue letter chip contrast', () => {
	it('reuses the exact floor main waveform cues enforce', () => {
		assert.equal(cuesMod.CUE_MIN_CONTRAST, 4.5);
	});

	it('white on dark --rb-green fails the floor (the pre-fix bug)', () => {
		const tokens = ccMod.parseColorTokens(THEME_CSS, DARK_SELECTOR);
		const darkGreen = tokens['rb-green'];
		assert.ok(darkGreen, 'dark theme must declare --rb-green');
		const ratio = cuesMod.contrastRatio('#ffffff', darkGreen);
		assert.ok(
			ratio < cuesMod.CUE_MIN_CONTRAST,
			`expected white-on-green to fail the floor, got ${ratio.toFixed(3)}:1`
		);
	});

	it('.cue-letter background stays var(--rb-green)', () => {
		const { styles } = cueLetterContrast(DARK_SELECTOR);
		assert.equal(styles.background, 'var(--rb-green)');
	});

	it('.cue-letter colors clear the floor on both shipped palettes', () => {
		for (const [name, selector] of [
			['dark', DARK_SELECTOR],
			['light', LIGHT_SELECTOR]
		]) {
			const { ratio } = cueLetterContrast(selector);
			assert.ok(
				ratio >= cuesMod.CUE_MIN_CONTRAST,
				`${name} .cue-letter only clears ${ratio.toFixed(3)}:1, below ${cuesMod.CUE_MIN_CONTRAST}:1`
			);
		}
	});
});
