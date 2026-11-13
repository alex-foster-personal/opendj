/**
 * Beat FX STUTTER taste variants (issue #10226); stutter-fx-kernel.ts reads and re-exports them.
 *
 * Def only, no DSP, so settings/variant-families.ts can register the row without
 * pulling the kernel into the settings chunk (#13727).
 */
import { defineVariantSetting } from '$lib/settings/variant-setting';

export const STUTTER_CHUNK_VARIANT = defineVariantSetting({
	id: 'variant_fx_stutter_chunk',
	label: 'STUTTER FX default chunk',
	group: 'advanced',
	keywords: ['variant', 'fx', 'stutter', 'chunk', 'beat fx'],
	detail: 'Chunk length the beat FX STUTTER replays until a length is chosen (issue #10226).',
	variants: [
		{ value: '1/4', label: '1/4 beat', summary: 'quarter-beat stutter' },
		{ value: '1/8', label: '1/8 beat', summary: 'eighth-beat buzz' }
	],
	defaultValue: '1/4'
});

export const STUTTER_BLEND_VARIANT = defineVariantSetting({
	id: 'variant_fx_stutter_blend',
	label: 'STUTTER FX blend law',
	group: 'advanced',
	keywords: ['variant', 'fx', 'stutter', 'blend', 'gate', 'beat fx'],
	detail: 'How LEVEL/DEPTH mixes dry and the STUTTER buffer (issue #10226).',
	variants: [
		{
			value: 'equal-power',
			label: 'Equal power',
			summary: 'dry cos, buffer sin across LEVEL/DEPTH'
		},
		{
			value: 'dry-held',
			label: 'Dry held',
			summary: 'dry stays full to the centre, buffer full from the centre'
		}
	],
	defaultValue: 'equal-power'
});
