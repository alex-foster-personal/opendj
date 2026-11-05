/**
 * Mixer stem dial layout (the maintainer, Mon 2 Nov 2026): with the mixer's S button on,
 * a channel whose track has stems shows its stem level dials NEXT TO its EQ,
 * never instead of it. VOCAL / INST / BASS stand in a column beside HI / MID /
 * LOW, row for row, and DRUMS stands beside FILTER.
 *
 * Supersedes MIXUX-04's per-channel STEM button, which swapped HI/MID/LOW over
 * to stem levels in place: the dial under the pointer changed identity, and the
 * Ctrl-hold version of that swap ended on the very pointer press that grabbed
 * a dial, so a stem dial could not be turned at all.
 *
 * A control the bundle's layout does not carry (RoFormer has no BASS or DRUMS)
 * has no dial: its slot stays empty so the rows still line up.
 */

import type { StemControl } from '$lib/rb/stem-types';

/** The stem column beside HI / MID / LOW, top to bottom. */
export const STEM_DIAL_COLUMN: readonly StemControl[] = ['vocal', 'instrumental', 'bass'];

/** The stem dial beside FILTER. */
export const STEM_FILTER_PARTNER: StemControl = 'drums';

export const STEM_DIAL_LABELS: Record<StemControl, string> = {
	vocal: 'VOCAL',
	instrumental: 'INST',
	drums: 'DRUMS',
	bass: 'BASS',
	other: 'HARM'
};

export interface StemDialLayout {
	/** One entry per EQ row (HI, MID, LOW); null where the bundle has no such control. */
	column: (StemControl | null)[];
	/** DRUMS when the bundle has it, else null. */
	filterPartner: StemControl | null;
}

/** Which stem dials a channel shows for the controls its bundle carries. */
export function stemDialLayout(available: readonly StemControl[]): StemDialLayout {
	const have = new Set(available);
	return {
		column: STEM_DIAL_COLUMN.map((stem) => (have.has(stem) ? stem : null)),
		filterPartner: have.has(STEM_FILTER_PARTNER) ? STEM_FILTER_PARTNER : null
	};
}

/** True when the S button is on and this channel's stems are ready to drive. */
export function channelShowsStemDials(
	mixerStems: boolean,
	stems: { status: string; available_controls: readonly StemControl[] }
): boolean {
	return mixerStems && stems.status === 'ready' && stems.available_controls.length > 0;
}

// ----------------------------------------------------------- MIDI EQ mode

/**
 * MIXUX-04's fixed HI/MID/LOW -> stem mapping, now used only by a MIDI
 * controller's hardware EQ knobs while that channel's `stem_eq_mode` is on
 * (controller-pad-runtime.svelte.ts). The on-screen mixer no longer swaps.
 */
export type EqDial = 'high' | 'mid' | 'low';

export interface StemDialAssignment {
	high: StemControl | null;
	mid: StemControl | null;
	low: StemControl | null;
}

export function stemDialAssignment(available: readonly StemControl[]): StemDialAssignment {
	const have = new Set(available);
	return {
		high: have.has('vocal') ? 'vocal' : null,
		mid: have.has('instrumental') ? 'instrumental' : null,
		low: have.has('drums') ? 'drums' : null
	};
}
