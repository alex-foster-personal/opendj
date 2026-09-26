/**
 * Gig helper opt-in dialog visibility (PERFMODE-16 v1).
 */

import type { AppPosturePref } from './app-posture-prefs';
import type { GigHelperPref } from './gig-helper-prefs';
import { shouldOfferGigHelper } from './gig-helper-prompt';

export let gigHelperPromptVisible = $state(false);

export function tryOfferGigHelperPromptOnPostureChange(
	previous: AppPosturePref,
	next: AppPosturePref,
	gigHelper: GigHelperPref
): void {
	if (previous !== 'gig' && next === 'gig' && shouldOfferGigHelper(next, gigHelper)) {
		gigHelperPromptVisible = true;
	}
}

export function dismissGigHelperPrompt(): void {
	gigHelperPromptVisible = false;
}
