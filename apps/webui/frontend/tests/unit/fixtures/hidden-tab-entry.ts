/**
 * Shared-graph entry for the hidden-tab deck-state test: the engine and the
 * playing gate must come from ONE bundle, or `anyDeckPlaying()` would read a
 * second copy of the deck state the engine never writes.
 */
export * as audio from '$lib/rb/audio-engine.svelte';
export * as gate from '$lib/rb/playing-gate';
export * as stores from '$lib/stores.svelte';
export * as registry from '$lib/rb/audio-context-registry';
export * as perf from '$lib/rb/perf-event-log';
export * as health from '$lib/rb/audio-health.svelte';
