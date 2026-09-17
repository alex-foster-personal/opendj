/**
 * Shared-graph entry for rescue ring writer tests.
 *
 * Re-export uiPrefs from the same bundle as the writer so posture guards
 * observe the test's mutations.
 */
export * from '$lib/rb/rescue-ring-writer.svelte';
export { uiPrefs } from '$lib/rb/prefs.svelte';
