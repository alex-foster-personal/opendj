/**
 * Shared-graph entry point for the setup-overlay tests.
 *
 * Same reason first-run-entry.ts exists: loadTypeScriptModule bundles each
 * entry independently, so importing detect-view.ts separately from
 * setup-api.ts would give the test one isFatalBlocker and the view rules a
 * different one. detect-view reads that function to decide a sentence's
 * TONE, so the two have to come from the same module instance or the severity
 * test proves nothing.
 *
 * No stubs: every module is the real thing and the only seam is
 * globalThis.fetch.
 */
export * from '$lib/api/capabilities.svelte';
export * from '$lib/setup/detect-view';
export * from '$lib/setup/overlay.svelte';
export * from '$lib/setup/present';
export * from '$lib/setup/setup-api';
export * from '$lib/setup/wizard.svelte';
