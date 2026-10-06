import type { StemControl } from '$lib/rb/stem-types';

/** Default (and Light) stem colours. Vocal matches wave/render.ts VOCAL_BLUE.
 * Every stem has its own hue: `other` (HARM, the demucs4 harmonics part) used
 * to share INST's green, so the two chips and dials could not be told apart.
 * Skins restyle these through the --rb-stem-<id> tokens in theme.css. */
export const STEM_COLORS = {
	vocal: '#4fb2ff',
	instrumental: '#9ad67a',
	drums: '#e0a35c',
	bass: '#b8a2ec',
	other: '#5cc8b0'
} as const;

/** CSS colour for a stem: the active skin's --rb-stem-<id> token, else STEM_COLORS. */
export function stemCssColor(stem: StemControl): string {
	return `var(--rb-stem-${stem}, ${STEM_COLORS[stem]})`;
}

/** Canvas colour for a stem from the token's computed value ('' when the skin leaves it unset). */
export function stemColorFromToken(stem: StemControl, computedToken: string): string {
	const token = computedToken.trim();
	return token === '' ? STEM_COLORS[stem] : token;
}
