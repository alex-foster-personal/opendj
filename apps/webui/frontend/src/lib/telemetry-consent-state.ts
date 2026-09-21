/**
 * The consent dialog's state, read by the overlay component and written by
 * `telemetry-consent.ts` (OBS-05).
 *
 * It lives in its own module on purpose: the overlay reads it, and the
 * consent module mounts the overlay (a dynamic import, so the component is
 * fetched only for a tester who has not answered). If the overlay imported
 * the consent module for these readers, the two would form an import cycle,
 * which the quality ratchet counts. Framework-free, like quit-gate-state.
 */

import type { components } from './api-types';

export type ConsentOut = components['schemas']['ConsentOut'];

let dialogOpen = false;
let current: ConsentOut | null = null;
const openListeners = new Set<() => void>();

export function setConsentDialogOpen(next: boolean): void {
	if (dialogOpen === next) return;
	dialogOpen = next;
	for (const listener of openListeners) listener();
}

export function isConsentDialogOpen(): boolean {
	return dialogOpen;
}

export function subscribeConsentDialogOpen(listener: () => void): () => void {
	openListeners.add(listener);
	return () => {
		openListeners.delete(listener);
	};
}

/** The last answer from the engine, for the dialog to read the terms version. */
export function currentConsent(): ConsentOut | null {
	return current;
}

export function setCurrentConsent(next: ConsentOut | null): void {
	current = next;
}
