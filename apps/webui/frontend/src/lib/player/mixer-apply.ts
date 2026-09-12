/**
 * LATENCY-01: immediate mixer apply stages, with no AudioContext import.
 *
 * Extracted from `audio-engine.svelte.ts` under convention D5 so filter, fader,
 * crossfader, and stem mute/solo can log `press_to_apply_ms` without growing
 * the engine hotspot.
 */

export const FILTER_APPLY_KIND = 'filter-apply';
export const FADER_APPLY_KIND = 'fader-apply';
export const XFADER_APPLY_KIND = 'xfader-apply';
export const STEM_MUTE_APPLY_KIND = 'stem-mute-apply';
export const STEM_SOLO_APPLY_KIND = 'stem-solo-apply';

export function mixerApplyStages(input: { pressToApplyMs: number }): Record<string, number> {
	return { press_to_apply_ms: input.pressToApplyMs };
}
