/**
 * Fixed Pioneer/Rekordbox HI/MID/LOW -> stem slot mapping for MIXUX-04.
 * Missing stems leave that dial as EQ; no packing leftover stems into empty knobs.
 */

import type { StemControl } from '$lib/rb/stem-types';

export type EqDial = 'high' | 'mid' | 'low';

export interface StemDialAssignment {
	high: StemControl | null;
	mid: StemControl | null;
	low: StemControl | null;
}

export const STEM_DIAL_SLOTS: Record<EqDial, StemControl> = {
	high: 'vocal',
	mid: 'instrumental',
	low: 'drums'
};

export const STEM_DIAL_LABELS: Record<StemControl, string> = {
	vocal: 'VOCAL',
	instrumental: 'INST',
	drums: 'DRUMS',
	bass: 'BASS',
	other: 'HARM'
};

export function stemDialAssignment(available: readonly StemControl[]): StemDialAssignment {
	const have = new Set(available);
	return {
		high: have.has('vocal') ? 'vocal' : null,
		mid: have.has('instrumental') ? 'instrumental' : null,
		low: have.has('drums') ? 'drums' : null
	};
}
