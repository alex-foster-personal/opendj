/**
 * Test entry for takeover-engine-sync.test.mjs: one esbuild bundle, so the
 * engine state the test mutates is the same instance the module under test
 * reads. Bundling the two modules separately would give each its own copy of
 * `mixerState` and `deckStates`.
 */
export { deckStates, mixerState } from '$lib/rb/audio-engine.svelte';
export { stemForEqBand } from '$lib/rb/midi/controller-pad-runtime.svelte';
export {
	eqBandSoftwareValue,
	softwareScalar,
	takeoverContinuous
} from '$lib/rb/midi/takeover-engine-sync.svelte';
