/** One bundle holding the real capture module and the engine stub it reads
 *  (aliased in for `$lib/rb/audio-engine.svelte`), so the test drives both. */
export * from '$lib/sets/master-mix-capture';
// @ts-expect-error resolved to tests/unit/fixtures/master-mix-engine-stub.ts by the test's alias
export { builds, setMasterMixTapPoint } from '$lib/rb/audio-engine.svelte';
