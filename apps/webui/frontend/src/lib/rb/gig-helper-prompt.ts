/**
 * Gig helper opt-in prompt (PERFMODE-16 v1).
 */

import type { AppPosturePref } from './app-posture-prefs';

export const GIG_HELPER_PREFS = ['unset', 'off', 'on'] as const;
export type GigHelperPref = (typeof GIG_HELPER_PREFS)[number];

export function shouldOfferGigHelper(posture: AppPosturePref, gigHelper: GigHelperPref): boolean {
	return posture === 'gig' && gigHelper === 'unset';
}
