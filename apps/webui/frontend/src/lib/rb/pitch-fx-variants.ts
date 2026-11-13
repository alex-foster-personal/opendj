/**
 * Beat FX PITCH taste variants (issue #10222); pitch-fx-kernel.ts reads and re-exports them.
 *
 * Def only, no DSP, so settings/variant-families.ts can register the row without
 * pulling the kernel into the settings chunk (#13727).
 */
import { defineVariantSetting } from '$lib/settings/variant-setting';

export const PITCH_FX_WINDOW_VARIANT = defineVariantSetting({
	id: 'variant_fx_pitch_window',
	label: 'PITCH FX window',
	group: 'advanced',
	keywords: ['variant', 'fx', 'pitch', 'window', 'beat fx'],
	detail: 'Crossfade window of the beat FX PITCH shifter (issue #10222).',
	variants: [
		{ value: '20', label: '20 ms', summary: 'tight, more warble' },
		{ value: '40', label: '40 ms', summary: 'balanced' },
		{ value: '80', label: '80 ms', summary: 'smooth, smears transients' }
	],
	defaultValue: '40'
});
