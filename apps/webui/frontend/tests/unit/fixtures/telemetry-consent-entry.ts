/** The consent module plus its dialog state, exposed together so the unit
 * test shares one bundled module instance of both. The state lives in its
 * own module so the overlay can read it without importing the module that
 * mounts the overlay (see agent-orders-entry.ts for the same need). */
export * from '$lib/telemetry-consent';
export * from '$lib/telemetry-scrub';
export { currentConsent, isConsentDialogOpen } from '$lib/telemetry-consent-state';
