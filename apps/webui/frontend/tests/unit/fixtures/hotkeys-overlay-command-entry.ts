/**
 * Shared-graph entry for hotkeys-overlay-command.test.mjs (#12272).
 *
 * loadTypeScriptModule bundles each entry on its own, so the dispatcher, the
 * overlay store the "?" key drives and the UI mirror must come from ONE bundle:
 * two copies of hotkeys-overlay.svelte.ts would be two overlays.
 */
export * as ipc from '$lib/rb/performance-ipc.svelte';
export * as guards from '$lib/rb/performance-ipc-guards';
export * as overlay from '$lib/components/rb/hotkeys/hotkeys-overlay.svelte';
export * as mirror from '$lib/rb/ui-mirror';
export { installRustEngineClientForTest } from '$lib/audio-engine/rust-engine';
export { initRustMode, rustMode } from '$lib/audio-engine/rust-mode.svelte';
export { deckStates } from '$lib/player/state.svelte';
