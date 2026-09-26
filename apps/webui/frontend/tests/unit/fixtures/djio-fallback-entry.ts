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
