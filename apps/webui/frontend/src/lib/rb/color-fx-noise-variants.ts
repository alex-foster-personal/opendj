/**
 * NOISE colour taste variant (#10270); color-fx-noise.ts reads and re-exports it.
 *
 * Def only, no DSP, so settings/variant-families.ts can register the row without
 * pulling the kernel into the settings chunk (#13727).
 */
import { defineVariantSetting } from '$lib/settings/variant-setting';

export const NOISE_COLOUR_VARIANT = defineVariantSetting({
	id: 'variant_sndcol_noise_colour',
	label: 'NOISE colour',
	group: 'advanced',
	keywords: ['noise', 'pink', 'white', 'sound color', 'sndcol'],
	detail: 'Spectrum of the NOISE kernel injected hiss.',
	variants: [
		{ value: 'white', label: 'White', summary: 'flat spectrum (DJM default)' },
		{ value: 'pink', label: 'Pink', summary: '-3 dB/oct darker riser' }
	],
	defaultValue: 'white'
});
