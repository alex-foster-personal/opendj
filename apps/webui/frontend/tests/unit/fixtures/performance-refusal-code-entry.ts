/**
 * Shared-graph entry for performance-refusal-code.test.mjs (#12280).
 *
 * loadTypeScriptModule bundles each entry on its own, so the command status,
 * the Rust engine seam and the UI mirror must come from ONE bundle: a test
 * that faked the engine client in one copy would never see a refusal the
 * other copy's dispatcher recorded.
 */
export * as ipc from '$lib/rb/performance-ipc.svelte';
export * as mirror from '$lib/rb/ui-mirror';
export * as refusal from '$lib/rb/performance-refusal-code';
export { EngineCommandError } from '$lib/audio-engine/client';
export { installRustEngineClientForTest } from '$lib/audio-engine/rust-engine';
export { initRustMode, rustMode } from '$lib/audio-engine/rust-mode.svelte';
export { deckStates } from '$lib/player/state.svelte';
