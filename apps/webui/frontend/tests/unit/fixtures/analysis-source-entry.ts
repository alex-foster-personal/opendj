/**
 * Shared-graph entry point for analysis-source.test.mjs.
 *
 * loadTypeScriptModule bundles each entry independently, so loading
 * analysis-source.svelte.ts and events-bus.ts as two separate entries yields two
 * separate buses - a test that delivers a frame through one and expects
 * `subscribeAnalysisRecordChanges` (which subscribes through the other) to hear
 * it silently proves nothing.
 *
 * Re-exporting both from a single entry puts them in one bundle, so `connect`
 * here opens the exact bus `subscribeKind` registered against. No stubs on
 * either side: the only injected seam is the bus's own documented
 * `socketFactory`, so a real `library.changed` envelope travels the real
 * dispatch path.
 */
export * from '$lib/rb/analysis-source.svelte';
// Same one-bundle argument as the bus below: the failed-switch rollback only
// exists when a REAL deck refresh really fails, which needs a real loaded deck
// in the SAME audio-engine instance analysis-source.svelte.ts calls into.
export { deckStates } from '$lib/rb/audio-engine.svelte';
export { connect as connectEventsBus } from '$lib/api/events-bus';
