/**
 * Shared-graph entry for TRANS-01 live query tests.
 *
 * loadTypeScriptModule bundles each entry independently, so loading
 * transition-read.svelte.ts and performance-ipc.svelte.ts as two separate
 * entries yields two copies of mixerState / deckStates. Re-exporting both
 * from a single entry puts them in one bundle, so the stores here are the
 * exact objects `queryPerformanceState` and `readTransition` close over.
 * No stubs.
 */
export { deckStates, mixerState } from '$lib/rb/audio-engine.svelte';
export { queryPerformanceState } from '$lib/rb/performance-ipc.svelte';
export { readTransition } from '$lib/rb/transition-read.svelte';
