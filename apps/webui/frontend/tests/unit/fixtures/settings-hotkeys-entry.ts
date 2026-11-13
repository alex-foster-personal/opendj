/**
 * Shared-graph entry point for settings-hotkeys.test.mjs.
 *
 * loadTypeScriptModule bundles each entry independently, so loading
 * settings/hotkeys.ts and settings/overlay.svelte.ts as two entries would give
 * two overlay stores: the listener would open one while the test read the
 * other. One entry puts both in one bundle, so isSettingsOpen reads the exact
 * store the installed listener toggles.
 */
export * from '$lib/settings/hotkeys';
export { isSettingsOpen } from '$lib/settings/overlay.svelte';
