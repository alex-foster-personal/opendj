/**
 * Edge sampler on/off chip state (spec edge-sampler 7.4, mock variant D `#sampToggle`).
 * The chip sits beside the waveform area's ZOOM control; titles and toasts are the mock's words.
 * Off: neither edge wakes, and samples already playing keep going (spec 7.4), so turning
 * the sampler off stops no voice.
 */
import { setSamplerEnabledPref, uiPrefs } from '$lib/rb/prefs.svelte';
import { pushToast } from '$lib/stores.svelte';

/** Hover title and accessible name of the chip for one state, verbatim from the mock. */
export function samplerToggleTitle(on: boolean): string {
	return on
		? 'Edge sampler ON: edges open on hover. Click to turn off.'
		: 'Edge sampler OFF: edges stay closed, playing samples keep going. Click to turn on.';
}

/** Toast shown after a click turned the sampler to `on`, verbatim from the mock. */
export function samplerToggleToast(on: boolean): string {
	return on ? 'Edge sampler on' : 'Edge sampler off: playing samples keep going';
}

/** Flip the persisted `sampler_enabled` pref, toast the new state, return it. */
export function toggleEdgeSampler(): boolean {
	const next = !uiPrefs.sampler_enabled;
	setSamplerEnabledPref(next);
	pushToast(samplerToggleToast(next));
	return next;
}
