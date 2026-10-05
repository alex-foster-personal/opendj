/**
 * Waveform band palette choice (issue #4219).
 *
 * The default is the rekordbox / CDJ "3Band" convention: dark blue LOW,
 * amber MID, white HIGH. AlphaTheta's CDJ-3000 tutorial: "Blue = low,
 * Amber = mid, White = high" (docs/research/dj-waveform-display-sota-20260922.md
 * section 4). The pre-#4219 palette (orange low, blue mid, near-white high)
 * had low and mid swapped against that; it stays selectable as 'legacy'.
 *
 * theme.css is still where the wavestack's CSS custom properties are
 * declared (`--rb-wave-low/mid/high/mono`, plus the
 * `html[data-wave-palette='legacy']` override blocks); this module is the TS
 * mirror the canvas painters that do not read CSS vars (the deck overview
 * strip and the library preview strip) use, and a drift test
 * (wave-palette.test.mjs) asserts both agree with theme.css.
 *
 * The light theme carries its own hues for both choices: a white HIGH band
 * vanishes on the light face, so light HIGH is a dark slate and every band is
 * darkened until it clears the 3:1 non-text floor (A11Y-03 pairings in
 * color-contrast.ts) against both light row backgrounds.
 */

export type WavePaletteChoice = 'rekordbox' | 'legacy' | 'mono';

const WAVE_PALETTE_CHOICES: readonly WavePaletteChoice[] = ['rekordbox', 'legacy', 'mono'];

export const WAVE_PALETTE_DEFAULT: WavePaletteChoice = 'rekordbox';

/** Undefined passes through (pref absent); any other non-choice throws. */
export function parseWavePalette(raw: unknown): WavePaletteChoice | undefined {
	if (raw === undefined) return undefined;
	if (!WAVE_PALETTE_CHOICES.includes(raw as WavePaletteChoice)) {
		throw new Error(`wave_palette must be rekordbox|legacy|mono, got ${String(raw)}`);
	}
	return raw as WavePaletteChoice;
}

export type WaveScheme = 'dark' | 'light';

export interface WaveBandColors {
	/** Lows band (--rb-wave-low). */
	low: string;
	/** Mids band (--rb-wave-mid). */
	mid: string;
	/** Highs band (--rb-wave-high). */
	high: string;
	/** Single-color 'mono' / 'line' designs (--rb-wave-mono). */
	mono: string;
	/** Vocal-presence bars (--rb-wave-vocal). */
	vocal: string;
}

/** Mirrors theme.css: `.perf-root`, `html[data-theme='light'] .perf-root`, and
 * the two `[data-wave-palette='legacy']` override blocks. */
export const WAVE_BAND_COLORS: Readonly<
	Record<WaveScheme, Readonly<Record<WavePaletteChoice, Readonly<WaveBandColors>>>>
> = {
	dark: {
		rekordbox: { low: '#2767d8', mid: '#f0a020', high: '#f4f6f8', mono: '#3d7dd9', vocal: '#4fb2ff' },
		legacy: { low: '#e8a13a', mid: '#3d7dd9', high: '#cfe0f2', mono: '#3d7dd9', vocal: '#4fb2ff' },
		mono: { low: '#6a6a6a', mid: '#b0b0b0', high: '#f4f4f4', mono: '#cfcfcf', vocal: '#cfe6ff' }
	},
	light: {
		rekordbox: { low: '#1d4fa3', mid: '#9a5a00', high: '#4a4f57', mono: '#2166b1', vocal: '#2a76c0' },
		legacy: { low: '#a44b11', mid: '#2166b1', high: '#3a4653', mono: '#2166b1', vocal: '#2a76c0' },
		mono: { low: '#6a6a6a', mid: '#4a4a4a', high: '#2a2a2a', mono: '#3a3a3a', vocal: '#3f6f9f' }
	}
};

export function resolveWaveBandColors(
	scheme: WaveScheme,
	choice: WavePaletteChoice = WAVE_PALETTE_DEFAULT
): WaveBandColors {
	const colors = WAVE_BAND_COLORS[scheme]?.[choice];
	if (colors === undefined) {
		throw new Error(`wave palette: no colors for scheme=${scheme} choice=${choice}`);
	}
	return { ...colors };
}

function _rgba(hex: string, alpha: number): string {
	const r = parseInt(hex.slice(1, 3), 16);
	const g = parseInt(hex.slice(3, 5), 16);
	const b = parseInt(hex.slice(5, 7), 16);
	return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

/** Overview/preview strip band fills: the same hues, with the translucency
 * the strip has always used on mid (0.85) and high (0.9) so the stacked bars
 * read through each other. */
export function resolveStripBandColors(
	scheme: WaveScheme,
	choice: WavePaletteChoice = WAVE_PALETTE_DEFAULT
): WaveBandColors {
	const c = resolveWaveBandColors(scheme, choice);
	return { low: c.low, mid: _rgba(c.mid, 0.85), high: _rgba(c.high, 0.9), mono: c.mono, vocal: c.vocal };
}
