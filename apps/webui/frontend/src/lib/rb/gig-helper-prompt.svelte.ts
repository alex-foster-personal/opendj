/**
 * Gig helper opt-in dialog visibility (PERFMODE-16 v1).
 */

import type { AppPosturePref } from './app-posture-prefs';
import type { GigHelperPref } from './gig-helper-prefs';
import { shouldOfferGigHelper } from './gig-helper-prompt';

// Private rune plus a getter: Svelte 5 refuses to export reassigned $state
// (state_invalid_export), which broke every importer's compile.
let gigHelperPromptVisible = $state(false);

export function isGigHelperPromptVisible(): boolean {
	return gigHelperPromptVisible;
}

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
