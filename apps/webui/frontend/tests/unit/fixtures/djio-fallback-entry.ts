/**
 * Shared-graph entry for the IOPIN-12 djio fallback test.
 *
 * `loadTypeScriptModule` bundles each entry independently, so loading the
 * engine, the toast store and the output-status module as three entries would
 * give three unrelated copies of their state, and a test reading the toast
 * store could never see a toast the engine raised. One entry, one bundle.
 */
export * as audio from '$lib/rb/audio-engine.svelte';
export * as stores from '$lib/stores.svelte';
export * as status from '$lib/rb/audio-output-status.svelte';
export * as player from '$lib/player/state.svelte';
export * as registry from '$lib/rb/audio-context-registry';
export * as mirror from '$lib/rb/ui-mirror';
export * as instrumentation from '$lib/rb/audio-context-instrumentation';
// The processor factory the engine and the rebuild share (a stub when the test aliases it).
export * as stretch from '$lib/rb/stretch-adapter';
// Bug #58: the engine tags a recovery-caused stop here so AutoPlay stays armed.
export * as recoveryStop from '$lib/rb/engine-recovery-stop';
