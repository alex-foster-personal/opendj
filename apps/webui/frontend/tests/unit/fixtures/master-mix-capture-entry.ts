/** One bundle holding the real capture module and the engine stub it reads
 *  (aliased in for `$lib/rb/audio-engine.svelte`), so the test drives both.
 *  The stub is imported by its own path: the bundler resolves the alias to the
 *  same file, so this is the very module instance the capture module reads. */
export * from '$lib/sets/master-mix-capture';
export { builds, setMasterMixTapPoint } from './master-mix-engine-stub';
