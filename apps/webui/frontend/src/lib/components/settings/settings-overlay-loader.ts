/**
 * Lazy loading for the settings overlay, kept beside the component so the root
 * layout's wiring stays one import (the quality ratchet's fan-out gate).
 *
 * The overlay draws nothing while closed and its effects all wait for it to
 * open, so keeping it off the first paint changes nothing a user or an agent
 * can observe while it is closed. It is mounted only while open, from its own
 * chunk, and warmed once the boot window closes (prefetchSettingsOverlay, from
 * the layout's onMount), so the first open draws without waiting on a fetch.
 * This is the payback named for the PR #6205 raise in scripts/bundle-budget.mjs.
 *
 * A chunk that cannot be fetched is handed to the caller's reporter (the
 * layout raises an error toast) and the overlay is closed, so the next open is
 * a real open that imports and reports again instead of leaving an invisible
 * "open" overlay that swallows every later press.
 */
import { bootScheduler } from '$lib/rb/boot-scheduler';
import { closeSettings, settingsOverlay } from '$lib/settings/overlay.svelte';

type SettingsOverlayModule = typeof import('./SettingsOverlay.svelte');
let settingsOverlayModule: Promise<SettingsOverlayModule> | null = null;

/** Whether the root layout should mount the overlay at all. */
export function isSettingsOverlayOpen(): boolean {
	return settingsOverlay.open;
}

export function loadSettingsOverlay(
	reportLoadFailure: (error: unknown) => void
): Promise<SettingsOverlayModule> {
	if (settingsOverlayModule === null) {
		const attempt = import('./SettingsOverlay.svelte');
		settingsOverlayModule = attempt;
		attempt.catch((error: unknown) => {
			// Forget the failed attempt so the next open imports and reports again.
			if (settingsOverlayModule === attempt) settingsOverlayModule = null;
			closeSettings();
			reportLoadFailure(error);
		});
	}
	return settingsOverlayModule;
}

export function prefetchSettingsOverlay(reportLoadFailure: (error: unknown) => void): void {
	// Warm the overlay chunk after boot; panel-offset loads on first open via overlay.svelte.ts.
	bootScheduler.defer('settings-overlay:prefetch', () => void loadSettingsOverlay(reportLoadFailure));
}
