/**
 * COLORFX-10320: the ECHO division picker in the color FX menu (1/4, 1/2, 3/4, 1 beat).
 *
 * Pure labels and timing for the menu's second group. The division set and its
 * 3/4 default stay `COLOR_FX_ECHO_BEATS` / `DEFAULT_COLOR_FX_SETTINGS`: an Open
 * DJ choice, since the DJM does not publish its DUB ECHO time.
 */

import { COLOR_FX_ECHO_BEATS, type ColorFxEchoBeats, type ColorFxEffect } from './color-fx';

/** The menu label of each division, in beats. */
export const COLOR_FX_DIVISION_LABELS: Readonly<Record<ColorFxEchoBeats, string>> = Object.freeze({
	0.25: '1/4',
	0.5: '1/2',
	0.75: '3/4',
	1: '1'
});

/** The divisions, in menu order. */
export const COLOR_FX_DIVISIONS: readonly ColorFxEchoBeats[] = COLOR_FX_ECHO_BEATS;

/** The division group shows only while the channel is on ECHO; the setting stays sticky either way. */
export function showsEchoDivisions(effect: ColorFxEffect): boolean {
	return effect === 'echo';
}

/** Milliseconds per repeat at `bpm`, rounded; null when the deck has no tempo. */
export function echoDivisionMs(beats: ColorFxEchoBeats, bpm: number | null): number | null {
	if (bpm === null || !Number.isFinite(bpm) || bpm <= 0) return null;
	return Math.round((60 / bpm) * beats * 1000);
}

/** Hover title of one division item: its ms at the deck's tempo, or the no-tempo reason. */
export function echoDivisionTitle(beats: ColorFxEchoBeats, bpm: number | null): string {
	const label = COLOR_FX_DIVISION_LABELS[beats];
	const ms = echoDivisionMs(beats, bpm);
	if (ms === null || bpm === null) return `ECHO ${label} beat - inert: this deck has no tempo`;
	return `ECHO ${label} beat at ${bpm.toFixed(1)} BPM = ${ms} ms per repeat`;
}
